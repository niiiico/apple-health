"""The interaction layer: one page, three write actions, two probes.

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

**No authentication.** It binds to the internal network and expects to sit
behind the estate's SSO the way other internal apps do. Do not expose it
directly; the write actions take no credential and assume the caller is you.
"""

from __future__ import annotations

import argparse
import http.server
import json
from datetime import date, datetime, timedelta
from typing import Any, Callable

from . import queries, ui
from .store import Store

WINDOW_DAYS = 45


def _as_date(value: Any, field: str) -> date:
    """Parse an ISO date, naming the field when it will not parse."""
    if not value:
        raise ValueError(f"{field} is required")
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        raise ValueError(f"{field} must be an ISO date, got {value!r}") from None


def set_zone_model(store: Store, payload: dict[str, Any]) -> dict[str, Any]:
    """Record the HR zone model in force from a date.

    Bands must ascend: an out-of-order model would silently misclassify every
    session it covers, and there is no later signal that it did.
    """
    effective_from = _as_date(payload.get("effective_from"), "effective_from")
    try:
        maxes = [int(payload[f"z{i}_max"]) for i in range(1, 5)]
    except (KeyError, TypeError, ValueError):
        raise ValueError("z1_max…z4_max are required and must be whole numbers") from None
    if not all(a < b for a, b in zip(maxes, maxes[1:])):
        raise ValueError(f"zone bounds must ascend, got {maxes}")

    with store.cursor() as cur:
        cur.execute(
            """INSERT INTO hr_zone_models (effective_from, source, z1_max, z2_max,
                   z3_max, z4_max, note)
               VALUES (%s,%s,%s,%s,%s,%s,%s)
               ON CONFLICT (effective_from) DO UPDATE SET
                   source = excluded.source, z1_max = excluded.z1_max,
                   z2_max = excluded.z2_max, z3_max = excluded.z3_max,
                   z4_max = excluded.z4_max, note = excluded.note""",
            (effective_from, payload.get("source") or "manual", *maxes,
             payload.get("note")),
        )
    store.commit()
    return {"message": f"zone model recorded from {effective_from.isoformat()}"}


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


ACTIONS: dict[str, Callable[[Store, dict[str, Any]], dict[str, Any]]] = {
    "set_zone_model": set_zone_model,
    "set_session_note": set_session_note,
    "set_period_note": set_period_note,
}


def render_page(store: Store, window_days: int = WINDOW_DAYS) -> str:
    """The single page: coverage, zone models, periods, recent sessions."""
    today = date.today()
    context = queries.context(store)
    sessions = queries.list_sessions(
        store, today - timedelta(days=window_days), today)["sessions"]
    return ui.render(context, list(reversed(sessions)), window_days)


def handler_for(dsn: str | None, window_days: int = WINDOW_DAYS):
    """Build the request handler.

    A store is opened per request rather than held: this serves one person on a
    LAN, and a long-lived connection through a Pi restart is more trouble than
    the handful of milliseconds it saves.
    """

    class Handler(http.server.BaseHTTPRequestHandler):
        def _send(self, code: int, body: bytes, content_type: str) -> None:
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _json(self, code: int, payload: dict) -> None:
            self._send(code, json.dumps(payload).encode(), "application/json")

        def do_GET(self):  # noqa: N802
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
            if self.path != "/":
                return self._send(404, b"not found", "text/plain; charset=utf-8")
            try:
                store = Store(dsn)
                try:
                    page = render_page(store, window_days)
                finally:
                    store.close()
            except Exception as exc:
                return self._send(500, f"{exc}".encode(), "text/plain; charset=utf-8")
            self._send(200, page.encode(), "text/html; charset=utf-8")

        def do_POST(self):  # noqa: N802
            action = ACTIONS.get(self.path.removeprefix("/api/"))
            if action is None:
                return self._json(404, {"error": f"no such action: {self.path}"})
            try:
                length = int(self.headers.get("Content-Length") or 0)
                payload = json.loads(self.rfile.read(length) or b"{}")
            except (ValueError, json.JSONDecodeError) as exc:
                return self._json(400, {"error": f"bad request body: {exc}"})

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
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=8087)
    ap.add_argument("--dsn", default=None, help="Defaults to APPLE_HEALTH_DSN.")
    ap.add_argument("--window-days", type=int, default=WINDOW_DAYS)
    args = ap.parse_args(argv)

    server = http.server.ThreadingHTTPServer(
        (args.host, args.port), handler_for(args.dsn, args.window_days))
    print(f"serving on http://{args.host}:{args.port}  (no auth — internal only)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
