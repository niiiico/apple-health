"""One full sync cycle: iCloud → inbox → health.db → session files → Vault.

Chains the pipeline in-process (no shell):

1. ``sources.icloud`` — mirror new files from the app's iCloud Drive folder.
2. ``sources.healthsync`` — merge pending deltas into ``health.db``.
3. ``sinks.session_files`` — re-render the last 14 days of session markdown.
4. ``sinks.vault_box`` — refresh the rolling Vault files + weekly brief.

The transport is iCloud (ADR-004); Box remains only as the *destination* of
step 4, the Claude Vault.

Steps 2–4 are skipped when step 1 fetched nothing (pass ``--force`` to run
them anyway). Designed for launchd (see ``tools/launchd/``) but safe to run by
hand; every step is idempotent.

Usage::

    uv run ah-sync [--inbox PATH] [--db PATH] [--force]
"""

from __future__ import annotations

import argparse
import contextlib
import io
import os
from datetime import date, timedelta
from pathlib import Path

from ..config import repo_root
from ..sources import healthsync
from ..sources import icloud
from . import pgsync
from ..sinks import session_files
from ..sinks import vault_box


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Run one iCloud→DB→Vault sync cycle.")
    ap.add_argument("--inbox", type=Path, default=icloud.DEFAULT_INBOX)
    ap.add_argument("--source", type=Path, default=None,
                    help="iCloud folder to mirror from (default: the app's container)")
    ap.add_argument("--db", type=Path,
                    default=repo_root() / "data/health.db")
    ap.add_argument("--force", action="store_true",
                    help="run ingest/render/push even when nothing new was fetched")
    args = ap.parse_args(argv)

    fetch_args = ["--inbox", str(args.inbox)]
    if args.source is not None:
        fetch_args += ["--source", str(args.source)]
    fetched = io.StringIO()
    with contextlib.redirect_stdout(fetched):
        rc = icloud.main(fetch_args)
    print(fetched.getvalue(), end="")
    if rc != 0:
        # A missing source folder or a failed copy must not look like a quiet,
        # healthy cycle — that is precisely how the DB went stale for 17 days
        # (ADR-004). Details are already on stderr.
        print(f"fetch failed (rc={rc}) — skipping ingest")
        return rc
    if not fetched.getvalue() and not args.force:
        print("nothing new")
        return 0

    healthsync.main(["--inbox", str(args.inbox), "--db", str(args.db)])
    # Keep Postgres level with SQLite before anything renders: the readers now
    # prefer it, and a store left behind would quietly render without zones.
    if os.environ.get("APPLE_HEALTH_DSN"):
        try:
            pgsync.main(["--inbox", str(args.inbox)])
        except Exception as exc:
            print(f"postgres sync failed ({exc}); renderers fall back to the inbox")
    since = (date.today() - timedelta(days=14)).isoformat()
    session_files.main(["--db", str(args.db), "--inbox", str(args.inbox),
                         "--since", since])
    vault_box.main(["--db", str(args.db), "--inbox", str(args.inbox)])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
