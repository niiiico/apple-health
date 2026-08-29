"""The interaction layer: one page, the advisor, and a conversation.

ADR-006 corollary (g). This is where the facts no sensor produces get recorded —
the zone model the watch was actually using, why a week went quiet, what a
session felt like. Without them the advisor reasons from numbers alone and
reproduces exactly the confident-wrong pattern the ADR exists to stop: it reads
four weeks of no swimming as lost fitness rather than a closed pool.

The shape follows `tvledger.web`, and for the reason its docstring gives:

- `ACTIONS` are plain functions of `(store, payload)`. Every rule the API
  enforces lives in them, and they can be called directly with no HTTP and no
  sockets — which is the only way they get tested.
- `ui.render` turns the store into one page.
- `handler_for` is the smallest HTTP layer that will serve both.

**No authentication of its own, by design.** An oauth2-proxy sidecar sharing
the pod's network namespace is the only thing the Service publishes, so every
request has already been through Authelia before this module sees it; who the
user is arrives as ``X-Forwarded-User``. That is why ``--host`` defaults to
loopback: binding every interface would publish the write actions — which take
no credential — to the rest of the cluster with the sidecar simply bypassed.
See ``deploy/k8s/``. Run it on 0.0.0.0 only on a machine you trust entirely.

Probes: ``/livez`` answers from the process alone, ``/healthz`` and ``/readyz``
reach Postgres. The distinction is not cosmetic — see ``do_GET``.
"""

from __future__ import annotations

import argparse
import http.server
import json
import threading
import time
import uuid
from datetime import date, datetime, timedelta
from typing import Any, Callable
from urllib.parse import parse_qs, urlparse

from . import queries, ui
from .store import Store

WINDOW_DAYS = 45


def _as_int(value: Any, field: str) -> int:
    """Parse a whole number, naming the field when it will not parse."""
    try:
        return int(value)
    except (TypeError, ValueError):
        raise ValueError(f"{field} must be a whole number, got {value!r}") from None


def _as_date(value: Any, field: str) -> date:
    """Parse an ISO date, naming the field when it will not parse."""
    if not value:
        raise ValueError(f"{field} is required")
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        raise ValueError(f"{field} must be an ISO date, got {value!r}") from None


def set_goal(store: Store, payload: dict[str, Any]) -> dict[str, Any]:
    """Record what you are training for, in your own words.

    Free text rather than a chosen kind: a race, a return-to-load rule and "stay
    consistent through the winter" are all goals, and an enum would be a branch
    in the code standing in for a sentence. `target_date` is optional because
    plenty of goals have no date, and a required one would invite a fake.
    """
    goal = (payload.get("goal") or "").strip()
    if not goal:
        raise ValueError("goal text is required")
    target = payload.get("target_date") or None
    # A past target_date is accepted: a goal whose date has gone is a real
    # state, and the advisor should say the race has been run rather than be
    # prevented from ever seeing it.
    target_date = _as_date(target, "target_date") if target else None

    with store.cursor() as cur:
        cur.execute(
            "INSERT INTO goals (goal, target_date) VALUES (%s,%s) RETURNING id",
            (goal, target_date))
        goal_id = cur.fetchone()["id"]
    store.commit()
    when = f" by {target_date.isoformat()}" if target_date else ""
    return {"message": f"goal #{goal_id} recorded{when}"}


def archive_goal(store: Store, payload: dict[str, Any]) -> dict[str, Any]:
    """Retire a goal without deleting it.

    Archived rather than removed: a plan written towards a goal is only
    intelligible if the goal it was written towards still exists.
    """
    goal_id = _as_int(payload.get("id"), "id")
    with store.cursor() as cur:
        cur.execute("UPDATE goals SET archived_at = now()"
                    " WHERE id = %s AND archived_at IS NULL", (goal_id,))
        if cur.rowcount == 0:
            raise ValueError(f"no active goal #{goal_id}")
    store.commit()
    return {"message": f"goal #{goal_id} archived"}


def set_session_note(store: Store, payload: dict[str, Any]) -> dict[str, Any]:
    """Attach (or clear) the note on one session."""
    try:
        workout_id = int(payload["workout_id"])
    except (KeyError, TypeError, ValueError):
        raise ValueError("workout_id is required") from None
    note = (payload.get("note") or "").strip()

    with store.cursor() as cur:
        cur.execute("SELECT 1 FROM workouts WHERE id = %s", (workout_id,))
        if not cur.fetchone():
            raise ValueError(f"no workout with id {workout_id}")
        if not note:
            cur.execute("DELETE FROM session_notes WHERE workout_id = %s", (workout_id,))
            store.commit()
            return {"message": "note cleared"}
        cur.execute(
            """INSERT INTO session_notes (workout_id, note) VALUES (%s,%s)
               ON CONFLICT (workout_id) DO UPDATE SET
                   note = excluded.note, updated_at = now()""",
            (workout_id, note),
        )
    store.commit()
    return {"message": "saved"}


def set_period_note(store: Store, payload: dict[str, Any]) -> dict[str, Any]:
    """Record a span of context — a trip, a closed pool, an injury.

    Periods rather than weeks: the France block ran 16 Jul to 12 Aug and aligned
    to nothing.
    """
    starts_on = _as_date(payload.get("starts_on"), "starts_on")
    ends_on = _as_date(payload["ends_on"], "ends_on") if payload.get("ends_on") else None
    if ends_on and ends_on < starts_on:
        raise ValueError(f"period ends ({ends_on}) before it starts ({starts_on})")
    note = (payload.get("note") or "").strip()
    if not note:
        raise ValueError("note is required — an empty period records nothing")

    with store.cursor() as cur:
        cur.execute(
            "INSERT INTO period_notes (starts_on, ends_on, note) VALUES (%s,%s,%s)",
            (starts_on, ends_on, note),
        )
    store.commit()
    return {"message": "period recorded"}


# --- asking Claude -----------------------------------------------------------
# These run the Claude Code CLI, which takes a minute or two. The server is
# threaded, so a slow request occupies one thread rather than blocking the page;
# what it does need is an oauth2-proxy `--upstream-timeout` longer than the
# default 30s, or the proxy gives up before the model answers and it looks like
# an application fault. See deploy/k8s/apple-health.yaml.


def review_session(store: Store, payload: dict[str, Any]) -> dict[str, Any]:
    """Have the advisor review one session, and store the result.

    The model never writes: it returns text, and this stores it. Re-running
    replaces the previous review rather than adding a second.
    """
    from . import advisor

    workout_id = _as_int(payload.get("workout_id"), "workout_id")
    with store.cursor() as cur:
        cur.execute("SELECT 1 FROM workouts WHERE id = %s", (workout_id,))
        if cur.fetchone() is None:
            raise ValueError(f"no session {workout_id}")

    run = advisor.run_claude(advisor.REVIEW_TASK.format(workout_id=workout_id))
    advisor.store_review(store, workout_id, run)
    return {"message": f"analysée ({len(run.calls)} requêtes)", "review": run.text}


def write_plan(store: Store, payload: dict[str, Any]) -> dict[str, Any]:
    """Rewrite the standing plan from the goals and recent training."""
    from . import advisor
    from datetime import date as _date

    run = advisor.run_claude(advisor.PLAN_TASK.format(today=_date.today().isoformat()))
    advisor.store_plan(store, run)
    return {"message": f"plan réécrit ({len(run.calls)} requêtes)"}


def chat(store: Store, payload: dict[str, Any]) -> dict[str, Any]:
    """One conversational turn.

    `session_id` threads the conversation. It is the CLI's own, kept in the
    pod's filesystem, so the model's *memory* of a thread ends at a pod restart —
    but the transcript is stored here regardless, so what was said survives even
    when the thread it belonged to cannot be continued.
    """
    from . import advisor

    message = (payload.get("message") or "").strip()
    if not message:
        raise ValueError("message is required")
    if len(message) > 4000:
        raise ValueError("message is too long")

    session_id = payload.get("session_id") or None
    history = None
    if session_id:
        with store.cursor() as cur:
            cur.execute(
                """SELECT question, answer FROM chat_turns
                    WHERE session_id = %s ORDER BY asked_at""", (session_id,))
            history = [dict(r) for r in cur.fetchall()]

    # Reported as it arrives, so a two-minute answer is visibly forming rather
    # than a spinner that says nothing about whether anything is happening.
    progress = payload.get("_progress")
    run = advisor.chat(message, session_id, on_progress=progress, history=history)
    queries = [c.get("query") for c in run.calls]

    # Stored after the answer exists, never before: a question with no answer is
    # a worse record than no record. The session id groups a conversation; it is
    # the CLI's own, so a pod restart starts a new one and the old thread stays
    # readable rather than silently continuing under a dead id.
    with store.cursor() as cur:
        cur.execute(
            """INSERT INTO chat_turns (session_id, question, answer, queries, model)
               VALUES (%s,%s,%s,%s,%s)""",
            (run.session_id or "unknown", message, run.text,
             json.dumps(queries), advisor.MODEL))
    store.commit()

    return {"message": "", "reply": run.text, "session_id": run.session_id,
            "queries": queries}


ACTIONS: dict[str, Callable[[Store, dict[str, Any]], dict[str, Any]]] = {
    "review_session": review_session,
    "write_plan": write_plan,
    "chat": chat,
    "set_goal": set_goal,
    "archive_goal": archive_goal,
    "set_session_note": set_session_note,
    "set_period_note": set_period_note,
}


def window_for(params: dict[str, list[str]], window_days: int) -> tuple[date, date]:
    """The date range to show, from the query string or the default window.

    Unparseable dates fall back rather than erroring: a mistyped URL should show
    something, not a stack trace.
    """
    today = date.today()
    try:
        end = date.fromisoformat(params["to"][0])
    except (KeyError, IndexError, ValueError):
        end = today
    try:
        start = date.fromisoformat(params["from"][0])
    except (KeyError, IndexError, ValueError):
        start = end - timedelta(days=window_days)
    if start > end:
        start, end = end, start
    return start, end


def render_page(store: Store, start: date, end: date) -> str:
    """The index: coverage, window navigation, zone models, periods, sessions."""
    context = queries.context(store)
    sessions = queries.list_sessions(store, start, end)["sessions"]
    return ui.render(context, list(reversed(sessions)), start, end)


def render_session_page(store: Store, workout_id: int) -> str:
    """One session in full."""
    return ui.render_session(queries.session_detail(store, workout_id))


# Actions that ask Claude, and so take a minute or two. These are never run
# inside the request: a phone holding an HTTP connection open that long loses it
# to a screen lock, an app switch or a moment of bad signal, and Safari reports
# the loss as "TypeError: Load failed" with the work already half-done. They run
# in a background thread instead and the client polls, which survives all three.
SLOW = {"chat", "review_session", "write_plan"}

# Finished jobs are kept briefly so a client that blinked can still collect its
# answer. Deliberately in memory rather than a table: the *result* of every slow
# action is already persisted where it belongs — a chat turn, a review, the plan
# — so a lost job handle costs the answer's delivery, never the answer itself.
# Reloading the page finds it.
_JOBS: dict[str, dict[str, Any]] = {}
_JOBS_LOCK = threading.Lock()
_JOB_TTL_SECONDS = 1800


def _prune_jobs(now: float) -> None:
    """Forget finished jobs nobody collected. Called on each new job."""
    with _JOBS_LOCK:
        for key in [k for k, j in _JOBS.items()
                    if j.get("finished_at", now) < now - _JOB_TTL_SECONDS]:
            del _JOBS[key]


def start_job(dsn: str | None, name: str, payload: dict[str, Any]) -> str:
    """Run a slow action in the background; return the handle to poll."""
    job_id = uuid.uuid4().hex
    _prune_jobs(time.time())
    with _JOBS_LOCK:
        _JOBS[job_id] = {"state": "running", "action": name,
                         "started_at": time.time()}

    def note_progress(text: str, queries: list[str]) -> None:
        with _JOBS_LOCK:
            job = _JOBS.get(job_id)
            if job and job.get("state") == "running":
                job["partial"] = text
                job["queries"] = queries

    def run() -> None:
        # Its own Store: a psycopg connection belongs to one thread, and sharing
        # the request's would corrupt both.
        store = None
        outcome: dict[str, Any] = {
            "state": "failed", "error": "le job s'est arrêté sans rien dire"}
        try:
            store = Store(dsn)
            outcome = {"state": "done",
                       "result": ACTIONS[name](store, {**payload,
                                                       "_progress": note_progress})}
        except ValueError as exc:
            outcome = {"state": "failed", "error": str(exc)}
        except Exception as exc:                 # noqa: BLE001
            outcome = {"state": "failed",
                       "error": f"{exc.__class__.__name__}: {exc}"}
        finally:
            # Recording the outcome must not depend on the connection closing
            # cleanly. It used to: a close() that raised skipped the write and
            # left the job polling as "running" for ever, which is the one state
            # a client cannot recover from.
            if store is not None:
                for step in (store.rollback, store.close):
                    try:
                        if outcome["state"] == "done" and step is store.rollback:
                            continue
                        step()
                    except Exception:            # noqa: BLE001, S110
                        pass
            outcome["finished_at"] = time.time()
            with _JOBS_LOCK:
                _JOBS[job_id] = outcome

    threading.Thread(target=run, name=f"job-{name}", daemon=True).start()
    return job_id


def job_state(job_id: str) -> dict[str, Any]:
    """What a job is doing, or what it did."""
    with _JOBS_LOCK:
        job = _JOBS.get(job_id)
    if job is None:
        # Either it never existed or it has been pruned. Say which is possible
        # rather than implying the work failed — the answer is likely on the page.
        return {"state": "unknown",
                "error": "job inconnu — s'il a abouti, la réponse est sur la page "
                         "après rechargement"}
    if job["state"] == "running":
        return {"state": "running",
                "elapsed": round(time.time() - job["started_at"]),
                "partial": job.get("partial") or "",
                "queries": job.get("queries") or []}
    return job


def render_chats_page(store: Store) -> str:
    """The conversation list."""
    return ui.render_chats(queries.chat_sessions(store)["sessions"])


def render_chat_page(store: Store, session_id: str) -> str:
    """One conversation, full screen."""
    turns = queries.chat_history(store, session_id=session_id)["turns"]
    return ui.render_chat(session_id, turns)


def handler_for(dsn: str | None, window_days: int = WINDOW_DAYS):
    """Build the request handler.

    A store is opened per request rather than held: this serves one person on a
    LAN, and a long-lived connection through a Pi restart is more trouble than
    the handful of milliseconds it saves.
    """

    class Handler(http.server.BaseHTTPRequestHandler):
        # BaseHTTPRequestHandler defaults to HTTP/1.0, which closes the
        # connection after every response. oauth2-proxy speaks HTTP/1.1 and
        # pools connections, so it kept reusing sockets this server had already
        # closed: the request never arrived, the proxy answered 502 with an HTML
        # page, and the browser's JSON.parse on that HTML reported a "syntax
        # error" — three layers away from the cause.
        #
        # Safe because `_send` always sets Content-Length, which is what
        # keep-alive needs to find the end of a response.
        protocol_version = "HTTP/1.1"

        def _send(self, code: int, body: bytes, content_type: str) -> None:
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _json(self, code: int, payload: dict) -> None:
            self._send(code, json.dumps(payload).encode(), "application/json")

        def do_GET(self):  # noqa: N802
            parsed_early = urlparse(self.path)
            if parsed_early.path == "/api/job":
                job_id = (parse_qs(parsed_early.query).get("id") or [""])[0]
                return self._json(200, job_state(job_id))
            if self.path == "/livez":
                # Deliberately does not touch Postgres. Liveness asks only
                # whether this process is still serving; a database outage
                # should take the pod out of service, not restart it, because
                # restarting cannot fix somebody else's database and with one
                # replica it just keeps the site down longer.
                return self._json(200, {"ok": True})
            if self.path in ("/healthz", "/readyz"):
                try:
                    store = Store(dsn)
                    try:
                        observed = store.coverage().observed_through
                    finally:
                        store.close()
                except Exception as exc:
                    return self._json(503, {"ok": False, "error": str(exc)})
                return self._json(200, {"ok": True, "observed_through":
                                        observed.isoformat() if observed else None})
            parsed = urlparse(self.path)
            if parsed.path == "/chat" or parsed.path == "/chat/":
                render = render_chats_page
            elif parsed.path == "/chat/new":
                render = lambda store: ui.render_chat(None, [])  # noqa: E731
            elif parsed.path.startswith("/chat/"):
                sid = parsed.path.removeprefix("/chat/")
                render = lambda store: render_chat_page(store, sid)  # noqa: E731
            elif parsed.path.startswith("/session/"):
                try:
                    workout_id = int(parsed.path.removeprefix("/session/"))
                except ValueError:
                    return self._send(404, b"not found", "text/plain; charset=utf-8")
                render = lambda store: render_session_page(store, workout_id)  # noqa: E731
            elif parsed.path == "/":
                start, end = window_for(parse_qs(parsed.query), window_days)
                render = lambda store: render_page(store, start, end)  # noqa: E731
            else:
                return self._send(404, b"not found", "text/plain; charset=utf-8")
            try:
                store = Store(dsn)
                try:
                    page = render(store)
                finally:
                    store.close()
            except Exception as exc:
                return self._send(500, f"{exc}".encode(), "text/plain; charset=utf-8")
            self._send(200, page.encode(), "text/html; charset=utf-8")

        def do_POST(self):  # noqa: N802
            action = ACTIONS.get(urlparse(self.path).path.removeprefix("/api/"))
            if action is None:
                return self._json(404, {"error": f"no such action: {self.path}"})
            try:
                length = int(self.headers.get("Content-Length") or 0)
                payload = json.loads(self.rfile.read(length) or b"{}")
            except (ValueError, json.JSONDecodeError) as exc:
                return self._json(400, {"error": f"bad request body: {exc}"})

            # Slow actions answer immediately with a handle and do the work
            # behind the request, so no phone has to hold a connection open for
            # two minutes to receive an answer.
            name = urlparse(self.path).path.removeprefix("/api/")
            if name in SLOW:
                try:
                    return self._json(202, {"job": start_job(dsn, name, payload)})
                except Exception as exc:         # noqa: BLE001
                    return self._json(500, {"error": f"{exc.__class__.__name__}: {exc}"})

            store = Store(dsn)
            try:
                result = action(store, payload)
            except ValueError as exc:            # a rule the caller broke
                store.rollback()
                return self._json(400, {"error": str(exc)})
            except Exception as exc:             # anything else is ours
                store.rollback()
                return self._json(500, {"error": f"{exc.__class__.__name__}: {exc}"})
            finally:
                store.close()
            self._json(200, result)

        def log_message(self, fmt, *args):
            print(f"{self.address_string()} {fmt % args}")

    return Handler


def main(argv: list[str] | None = None) -> int:
    """Serve the interaction layer."""
    ap = argparse.ArgumentParser(description="Serve the health interaction layer.")
    # Loopback by default. In the cluster an oauth2-proxy sidecar shares this
    # pod's network namespace and upstreams to 127.0.0.1, so binding every
    # interface would publish the write API — which takes no credential —
    # to every other pod, with the sidecar simply bypassed. tvledger's manifest
    # carries the same note because it learned it the hard way.
    ap.add_argument("--host", default="127.0.0.1",
                    help="Bind address. Keep loopback wherever a proxy fronts this.")
    ap.add_argument("--port", type=int, default=8087)
    ap.add_argument("--dsn", default=None, help="Defaults to APPLE_HEALTH_DSN.")
    ap.add_argument("--window-days", type=int, default=WINDOW_DAYS)
    args = ap.parse_args(argv)

    server = http.server.ThreadingHTTPServer(
        (args.host, args.port), handler_for(args.dsn, args.window_days))
    print(f"serving on http://{args.host}:{args.port}  (no auth of its own — "
          f"expects a proxy in front)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
