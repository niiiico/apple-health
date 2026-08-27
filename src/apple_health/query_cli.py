"""``ah-query`` — the read surface as a command, for Claude Code to call.

The advisor drives the Claude Code CLI rather than the Messages API (the house
arrangement, as biblio and braid use), so its tools have to be *commands*. This
is that: one subcommand per function in `queries`, printing JSON on stdout.

It is the only tool the advisor is permitted, which is what keeps the model
read-only. There is deliberately no subcommand that writes: reviews and plans
are stored by the driver from the text the model returns, so a confabulation
cannot become a stored fact in one step.

**Every call records itself.** With ``AH_QUERY_LOG`` set to a path, each
invocation appends a JSON line naming the subcommand and its arguments. That is
where `session_reviews.basis` gets its record of what a review was actually
written from — read back from what ran, rather than parsed out of the CLI's
event stream, which would be a guess about someone else's output format.

Usage::

    ah-query context
    ah-query sessions --start 2026-08-01 --end 2026-08-27 [--activity Swimming]
    ah-query session --id 5561
    ah-query metric --metric RestingHeartRate --start 2026-06-01 --end 2026-08-27
    ah-query race [--race 2025-09-27-triathlon-olympique]
    ah-query reviews --start 2026-08-01 --end 2026-08-27
    ah-query doc [--slug kujukuri-2026]
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import date
from pathlib import Path
from typing import Any

from . import queries
from .store import Store

_LOG_VAR = "AH_QUERY_LOG"


def _record(command: str, args: dict[str, Any]) -> None:
    """Append this call to the run log, if one is configured.

    Failure to log is never fatal: a review that ran is worth more than a
    complete audit trail, and the missing line is visible in `basis` anyway.
    """
    path = os.environ.get(_LOG_VAR)
    if not path:
        return
    try:
        with Path(path).open("a") as fh:
            fh.write(json.dumps({"query": command, "args": args}) + "\n")
    except OSError:
        pass


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Query the training record. Every response states its own basis.")
    sub = ap.add_subparsers(dest="command", required=True)

    sub.add_parser("context", help="coverage, zone bands, goals, period notes")

    s = sub.add_parser("sessions", help="every session in a window, no filtering")
    s.add_argument("--start", type=date.fromisoformat, required=True)
    s.add_argument("--end", type=date.fromisoformat, required=True)
    s.add_argument("--activity", default=None)

    d = sub.add_parser("session", help="one session in full")
    d.add_argument("--id", type=int, required=True)

    m = sub.add_parser("metric", help="one metric over time, bucketed")
    m.add_argument("--metric", required=True)
    m.add_argument("--start", type=date.fromisoformat, required=True)
    m.add_argument("--end", type=date.fromisoformat, required=True)
    m.add_argument("--bucket", default="week", choices=("week", "month"))

    r = sub.add_parser("race", help="archived race breakdowns")
    r.add_argument("--race", default=None)

    v = sub.add_parser("reviews", help="reviews already written for a window")
    v.add_argument("--start", type=date.fromisoformat, required=True)
    v.add_argument("--end", type=date.fromisoformat, required=True)

    doc = sub.add_parser("doc", help="reference documents; no argument lists them")
    doc.add_argument("--slug", default=None)

    args = ap.parse_args(argv)

    # race_detail reads files, not the database, so it needs no connection —
    # and must keep working if Postgres is unreachable.
    if args.command == "race":
        _record("race", {"race": args.race})
        json.dump(queries.race_detail(args.race), sys.stdout, default=str, indent=1)
        print()
        return 0

    store = Store(None)
    try:
        if args.command == "context":
            _record("context", {})
            out = queries.context(store)
        elif args.command == "sessions":
            payload = {"start": args.start.isoformat(), "end": args.end.isoformat(),
                       "activity": args.activity}
            _record("sessions", payload)
            out = queries.list_sessions(store, args.start, args.end, args.activity)
        elif args.command == "session":
            _record("session", {"id": args.id})
            out = queries.session_detail(store, args.id)
        elif args.command == "reviews":
            payload = {"start": args.start.isoformat(), "end": args.end.isoformat()}
            _record("reviews", payload)
            out = queries.reviews(store, args.start, args.end)
        elif args.command == "doc":
            _record("doc", {"slug": args.slug})
            out = queries.document(store, args.slug)
        else:
            payload = {"metric": args.metric, "start": args.start.isoformat(),
                       "end": args.end.isoformat(), "bucket": args.bucket}
            _record("metric", payload)
            out = queries.metric_history(store, args.metric, args.start, args.end,
                                         args.bucket)
    finally:
        store.close()

    json.dump(out, sys.stdout, default=str, indent=1)
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
