"""One full sync cycle: iCloud → inbox → health.db → session files → Vault.

Chains the pipeline in-process (no shell):

1. ``icloud_fetch`` — mirror new files from the app's iCloud Drive folder.
2. ``ah-ingest`` — merge pending deltas into ``health.db``.
3. ``session_detail`` — re-render the last 14 days of session markdown.
4. ``vault_push`` — refresh the rolling Vault files + weekly brief.

The transport is iCloud (ADR-004); Box remains only as the *destination* of
step 4, the Claude Vault.

Steps 2–4 are skipped when step 1 fetched nothing (pass ``--force`` to run
them anyway). Designed for launchd (see ``tools/launchd/``) but safe to run by
hand; every step is idempotent.

Usage::

    uv run python tools/sync_cycle.py [--inbox PATH] [--db PATH] [--force]
"""

from __future__ import annotations

import argparse
import contextlib
import io
import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
from apple_health import ingest  # noqa: E402
import icloud_fetch  # noqa: E402
import session_detail  # noqa: E402
import vault_push  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Run one iCloud→DB→Vault sync cycle.")
    ap.add_argument("--inbox", type=Path, default=icloud_fetch.DEFAULT_INBOX)
    ap.add_argument("--db", type=Path,
                    default=Path(__file__).resolve().parent.parent / "data/health.db")
    ap.add_argument("--force", action="store_true",
                    help="run ingest/render/push even when nothing new was fetched")
    args = ap.parse_args(argv)

    fetched = io.StringIO()
    with contextlib.redirect_stdout(fetched):
        icloud_fetch.main(["--inbox", str(args.inbox)])
    print(fetched.getvalue(), end="")
    if not fetched.getvalue() and not args.force:
        print("nothing new")
        return 0

    ingest.main(["--inbox", str(args.inbox), "--db", str(args.db)])
    since = (date.today() - timedelta(days=14)).isoformat()
    session_detail.main(["--db", str(args.db), "--inbox", str(args.inbox),
                         "--since", since])
    vault_push.main(["--db", str(args.db), "--inbox", str(args.inbox)])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
