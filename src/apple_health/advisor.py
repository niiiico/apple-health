"""The advisor: read the record through `queries`, write an opinion about it.

ADR-006 corollary (e). It writes rather than chats, and that is the whole shape:
writing needs only outbound egress, so nothing has to be exposed inbound; the
output is readable from anywhere without the reader reaching this service; and
an opinion on disk can be checked later against the data it was formed from,
which one in a chat window cannot.

Two jobs, at two horizons:

- ``review`` — one session at a time, for sessions with no review yet.
- ``plan`` — a standing document, rewritten from the goals and recent reviews.

The second is built on the first. Reviews are the raw material; the plan is what
they add up to.

**The model gets read tools only.** Every write in this module is done by the
driver, from a tool result the model produced as *text*. That is deliberate: an
advisor is a machine for producing fluent, confident prose, which is precisely
the failure this project has spent six ADRs stamping out. Giving it a write tool
would let a confabulation become a stored fact in one step.

**Everything it sees comes through `queries`**, never raw SQL. Those responses
carry their own coverage boundary and zone bands, so the model cannot read "no
swimming in four weeks" without also being told how far the record is known to
extend. `basis` records what it actually looked at, so a review that turns out
to be wrong can be traced to what it was working from.

Usage::

    uv run ah-advise review [--limit N] [--since YYYY-MM-DD] [--dry-run]
    uv run ah-advise plan [--dry-run]
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from . import queries
from .derive.zones import ZONES
from .store import Store

MODEL = "claude-opus-5"
_KEY_VAR = "ANTHROPIC_API_KEY"
_KEY_FILE = Path.home() / ".config/apple-health/anthropic-key"

# How far back `review` looks for unreviewed sessions by default. Not a filter
# on what is worth reviewing — every session in the window gets one, however
# short — but a bound on how much work one run does. Sessions older than this
# are counted and reported rather than silently skipped.
DEFAULT_REVIEW_DAYS = 14

# Turns before the loop gives up. Reached only if the model keeps calling tools
# without concluding; the run then fails rather than storing a partial opinion.
MAX_TURNS = 12

PLAN_SLUG = "plan"


def _api_key() -> str:
    """The API key, from the environment or the same 0600 file the DB uses."""
    from_env = os.environ.get(_KEY_VAR)
    if from_env:
        return from_env
    try:
        key = _KEY_FILE.read_text().strip()
    except OSError:
        key = ""
    if not key:
        raise SystemExit(
            f"No API key. Set {_KEY_VAR} or write it to {_KEY_FILE} (mode 0600)."
        )
    return key


# --- the tools ---------------------------------------------------------------
# Descriptions are written for the model, and say what each answer does *not*
# cover as much as what it does. A tool described only by its happy path is how
# an absent row becomes a confident zero.

TOOL_SPECS: list[dict[str, Any]] = [
    {
        "name": "context",
        "description": (
            "Orient before anything else. Returns how far the record is known to "
            "extend (the coverage boundary), the heart-rate zone bands every zone "
            "figure is computed with, the athlete's recorded goals in their own "
            "words, and period notes explaining gaps. Call this first, always: "
            "the other tools return numbers that are meaningless without it."
        ),
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "list_sessions",
        "description": (
            "Every session in a date window, most recent first. No distance floor "
            "and no filtering by interest: a 9-minute walk is returned alongside a "
            "two-hour ride, because deciding what counts is the caller's job and "
            "silently dropping the short ones is how a training week reads as "
            "empty. Compact rows; use session_detail for one that matters."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "start": {"type": "string", "description": "ISO date, inclusive"},
                "end": {"type": "string", "description": "ISO date, inclusive"},
                "activity": {
                    "type": "string",
                    "description": "Optional exact activity, e.g. Running, Swimming, Cycling",
                },
            },
            "required": ["start", "end"],
        },
    },
    {
        "name": "session_detail",
        "description": (
            "One session in full: heart-rate zone durations and shares, drift "
            "across thirds, laps if the source recorded any, route splits, and the "
            "athlete's own note. States explicitly when a session has no "
            "heart-rate series rather than reporting zeroes for it."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"workout_id": {"type": "integer"}},
            "required": ["workout_id"],
        },
    },
    {
        "name": "metric_history",
        "description": (
            "One metric over time, bucketed by week or month — resting heart rate, "
            "HRV, VO2max, body mass and so on. Reports which underlying source it "
            "read and how far that source covers, and warns when the source stops "
            "short of the window asked for. A short answer means the data stops, "
            "not that the value went to zero."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "metric": {
                    "type": "string",
                    "description": "e.g. RestingHeartRate, HeartRateVariabilitySDNN, VO2Max, BodyMass",
                },
                "start": {"type": "string", "description": "ISO date"},
                "end": {"type": "string", "description": "ISO date"},
                "bucket": {"type": "string", "enum": ["week", "month"]},
            },
            "required": ["metric", "start", "end"],
        },
    },
    {
        "name": "race_detail",
        "description": (
            "Archived per-leg race breakdowns, mined from the raw export and kept "
            "outside the database. Call with no argument to list which races have "
            "an archive; call with one to read it. These are the only place "
            "official splits and placings live."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"race": {"type": "string"}},
        },
    },
]


def _dispatch(store: Store, name: str, args: dict[str, Any]) -> dict[str, Any]:
    """Run one tool call. Errors are returned, not raised.

    A tool that raises ends the run; a tool that returns its error lets the
    model correct a bad argument and carry on, which is the common case.
    """
    try:
        if name == "context":
            return queries.context(store)
        if name == "list_sessions":
            return queries.list_sessions(
                store, date.fromisoformat(args["start"]), date.fromisoformat(args["end"]),
                args.get("activity"))
        if name == "session_detail":
            return queries.session_detail(store, int(args["workout_id"]))
        if name == "metric_history":
            return queries.metric_history(
                store, args["metric"], date.fromisoformat(args["start"]),
                date.fromisoformat(args["end"]), args.get("bucket") or "week")
        if name == "race_detail":
            return queries.race_detail(args.get("race"))
        return {"error": f"unknown tool {name!r}"}
    except Exception as exc:                      # noqa: BLE001 — reported to the model
        return {"error": f"{type(exc).__name__}: {exc}"}


SYSTEM = """You are advising one endurance athlete on their own training, reading \
their real training record through the tools provided.

Write for them directly, in the second person. Be concrete and brief. They have \
thirteen years of history and know their own sport — skip the generalities and \
the encouragement, and say the thing that is actually true of this data.

Rules that matter more than being helpful:

1. Call `context` first, every time. It gives you the coverage boundary, the \
zone bands, and their goals. Numbers from the other tools mean nothing without it.

2. Absence of data is never evidence of absence of training. If you see no \
swimming for four weeks, the possibilities are: they did not swim, the pool was \
shut, the sync is broken, or the window runs past the coverage boundary. Check \
`context` for a period note explaining it, and if there is none, say what you \
observed and that you cannot tell which — never "you have stopped swimming".

3. Never report a number the tools did not give you. Do not estimate a pace you \
were not shown, do not infer a heart rate from a distance, do not carry a figure \
over from your own knowledge of typical athletes. If you want a number, call a \
tool for it.

4. Zone figures come from one fixed set of bands, given to you by `context`. \
They are not read from the watch — nothing can read them from the watch — so if \
they look wrong for this athlete, say so rather than silently reinterpreting them.

5. If the data does not support a conclusion, say that. "The record does not show \
enough to tell" is a useful answer here and a wrong answer is not. You are not \
being scored on having an opinion."""

REVIEW_TASK = """Review this single session: workout id {workout_id}.

Call `session_detail` for it, and whatever else you need to judge it — the \
sessions around it, a metric trend, a past race. Then write the review.

Length should match what happened. An easy half-hour walk deserves one sentence. \
A hard interval session or a long ride deserves a short paragraph. Do not pad a \
quiet session into a paragraph, and do not compress a significant one into a line.

Cover, only where there is something to say: what the session was, how it went \
against the athlete's goals, anything notable in the zone distribution or drift, \
and anything that should change what they do next. If a session is unremarkable, \
saying so is the correct review.

Write only the review text. No preamble, no heading, no sign-off."""

PLAN_TASK = """Write the athlete's standing training plan, as of {today}.

Start with `context` to read their goals. Everything you write should serve those \
goals; if none are recorded, say plainly that no goal is recorded and that the \
plan is therefore a description of current training rather than a plan towards \
anything — do not invent an objective.

Look at recent training with `list_sessions`, at whatever trends matter with \
`metric_history`, and at past races with `race_detail` where a goal points at one.

Write in markdown, and keep it to something readable on a phone before a race. \
Suggested shape, to vary as the situation deserves:

- **Where you are** — an honest read of current training, including what the \
record cannot tell you.
- **The next few weeks** — what to do, concretely enough to act on.
- **Watch for** — anything in the data that warrants attention.

This document is rewritten in full each time, so it should read as current on its \
own, without reference to previous versions."""


@dataclass
class Run:
    """One completed conversation: the text produced, and what it looked at."""

    text: str
    calls: list[dict[str, Any]] = field(default_factory=list)
    input_tokens: int = 0
    output_tokens: int = 0

    def basis(self, store: Store) -> dict[str, Any]:
        """What the opinion was formed from, for storing beside it.

        A review is an opinion about data. Without the data it saw, it can be
        neither checked nor re-run, and in a year it is indistinguishable from a
        guess.
        """
        cov = store.coverage()
        return {
            "model": MODEL,
            "observed_through": (cov.observed_through.isoformat()
                                 if cov.observed_through else None),
            "zone_bands": {label: f"{lo}-{hi}" for label, lo, hi in ZONES},
            "tool_calls": self.calls,
            "usage": {"input": self.input_tokens, "output": self.output_tokens},
        }


def converse(client: Any, store: Store, task: str, effort: str = "medium") -> Run:
    """Run the tool loop until the model stops calling tools, and return its text.

    A manual loop rather than the SDK's tool runner, for one reason: every call
    has to be recorded into `basis`, and owning the loop is the plainest way to
    do that.
    """
    messages: list[dict[str, Any]] = [{"role": "user", "content": task}]
    calls: list[dict[str, Any]] = []
    n_in = n_out = 0

    for _ in range(MAX_TURNS):
        response = client.messages.create(
            model=MODEL,
            max_tokens=16000,
            system=SYSTEM,
            tools=TOOL_SPECS,
            output_config={"effort": effort},
            messages=messages,
        )
        n_in += response.usage.input_tokens
        n_out += response.usage.output_tokens

        if response.stop_reason == "max_tokens":
            # A truncated review reads as a finished one. Storing it would put a
            # sentence that stops mid-thought into the record as an opinion.
            raise RuntimeError("hit max_tokens before concluding; nothing stored")
        if response.stop_reason != "tool_use":
            text = "".join(b.text for b in response.content if b.type == "text").strip()
            if not text:
                raise RuntimeError(f"model stopped with {response.stop_reason!r} and no text")
            return Run(text, calls, n_in, n_out)

        messages.append({"role": "assistant", "content": response.content})
        results = []
        for block in response.content:
            if block.type != "tool_use":
                continue
            out = _dispatch(store, block.name, block.input or {})
            calls.append({"tool": block.name, "input": block.input})
            results.append({
                "type": "tool_result",
                "tool_use_id": block.id,
                "content": json.dumps(out, default=str),
            })
        messages.append({"role": "user", "content": results})

    raise RuntimeError(
        f"gave up after {MAX_TURNS} turns without a conclusion; nothing stored")


def unreviewed(store: Store, since: date, limit: int) -> tuple[list[dict], int]:
    """Sessions in [since, now] with no review, plus how many older ones exist.

    The older count is returned rather than swallowed: a run that quietly skips
    work looks identical to a run that had none to do.
    """
    with store.cursor() as cur:
        cur.execute(
            """SELECT w.id, w.activity, w.started_at
                 FROM workouts w
            LEFT JOIN session_reviews r ON r.workout_id = w.id
                WHERE r.workout_id IS NULL AND w.started_at >= %s
             ORDER BY w.started_at DESC
                LIMIT %s""",
            (since, limit))
        rows = [dict(r) for r in cur.fetchall()]
        cur.execute(
            """SELECT count(*) n
                 FROM workouts w
            LEFT JOIN session_reviews r ON r.workout_id = w.id
                WHERE r.workout_id IS NULL AND w.started_at < %s""",
            (since,))
        older = cur.fetchone()["n"]
    return rows, older


def store_review(store: Store, workout_id: int, run: Run) -> None:
    """Persist one review. Idempotent on workout_id, so a re-run replaces it."""
    with store.cursor() as cur:
        cur.execute(
            """INSERT INTO session_reviews (workout_id, review, model, basis)
               VALUES (%s,%s,%s,%s)
               ON CONFLICT (workout_id) DO UPDATE SET
                   review = excluded.review, model = excluded.model,
                   basis = excluded.basis, created_at = now()""",
            (workout_id, run.text, MODEL, json.dumps(run.basis(store))))
    store.commit()


def store_plan(store: Store, run: Run) -> None:
    """Persist the plan, replacing the previous one."""
    body = run.text + "\n\n---\n\n_" + _provenance(run, store) + "_\n"
    with store.cursor() as cur:
        cur.execute(
            """INSERT INTO documents (slug, body, volatility, updated_at)
               VALUES (%s,%s,'high',now())
               ON CONFLICT (slug) DO UPDATE SET
                   body = excluded.body, updated_at = now()""",
            (PLAN_SLUG, body))
    store.commit()


def _provenance(run: Run, store: Store) -> str:
    """A one-line footer saying what the plan was written from."""
    basis = run.basis(store)
    return (f"Written {datetime.now().astimezone():%Y-%m-%d %H:%M} by {MODEL} from a "
            f"record covered through {basis['observed_through']}, "
            f"using {len(run.calls)} queries.")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Write session reviews and the standing plan.")
    sub = ap.add_subparsers(dest="command", required=True)

    rev = sub.add_parser("review", help="review sessions that have none")
    rev.add_argument("--limit", type=int, default=10)
    rev.add_argument("--since", type=date.fromisoformat,
                     default=date.today() - timedelta(days=DEFAULT_REVIEW_DAYS))
    rev.add_argument("--dry-run", action="store_true", help="print, store nothing")
    rev.add_argument("--effort", default="medium")

    pl = sub.add_parser("plan", help="rewrite the standing plan")
    pl.add_argument("--dry-run", action="store_true", help="print, store nothing")
    pl.add_argument("--effort", default="high")

    args = ap.parse_args(argv)

    import anthropic
    client = anthropic.Anthropic(api_key=_api_key())
    store = Store(None)
    try:
        if args.command == "review":
            return _review(client, store, args)
        return _plan(client, store, args)
    finally:
        store.close()


def _review(client: Any, store: Store, args: argparse.Namespace) -> int:
    rows, older = unreviewed(store, args.since, args.limit)
    if older:
        print(f"note: {older} unreviewed session(s) before {args.since} were not "
              f"touched — pass --since to reach them", file=sys.stderr)
    if not rows:
        print("nothing to review")
        return 0

    failures = 0
    for row in rows:
        label = f"{row['started_at']:%Y-%m-%d %H:%M} {row['activity']} (#{row['id']})"
        try:
            run = converse(client, store, REVIEW_TASK.format(workout_id=row["id"]),
                           args.effort)
        except Exception as exc:                  # noqa: BLE001
            # One bad session must not abandon the rest; the review is simply
            # not stored, so the next run tries it again.
            print(f"FAILED {label}: {exc}", file=sys.stderr)
            failures += 1
            continue
        if args.dry_run:
            print(f"\n=== {label}\n{run.text}")
        else:
            store_review(store, row["id"], run)
            print(f"reviewed {label}  ({run.output_tokens} out, {len(run.calls)} queries)")
    return 1 if failures else 0


def _plan(client: Any, store: Store, args: argparse.Namespace) -> int:
    run = converse(client, store, PLAN_TASK.format(today=date.today().isoformat()),
                   args.effort)
    if args.dry_run:
        print(run.text)
        return 0
    store_plan(store, run)
    print(f"plan written ({run.output_tokens} out, {len(run.calls)} queries)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
