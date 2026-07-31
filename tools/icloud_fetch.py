"""Copy new HealthSync files from the iCloud Drive folder into the local inbox.

The iOS app writes delta JSON + sidecars into its ubiquity container
(``iCloud.net.dev2.healthsync/Documents/HealthSync``); the iCloud daemon syncs
them to this Mac. This step mirrors them into a durable inbox on the NAS —
iCloud evicts local copies of files it considers cold, and
``tools/session_detail.py`` needs the HR-series CSVs to stay readable long
after the delta itself was ingested.

Files iCloud has not materialised locally appear as ``.<name>.icloud``
placeholders. Reading the real path triggers the download and blocks until it
completes; anything still unavailable is left for the next cycle rather than
failing the run.

Copies are ordered sidecars-first, then deltas oldest-first, so a partially
copied inbox never contains a delta whose sidecars are missing (the same
ordering rule the producer follows — see docs/delta-contract.md).

Usage::

    uv run python tools/icloud_fetch.py [--inbox PATH] [--source PATH]
"""

from __future__ import annotations

import argparse
import shutil
import sys
from collections.abc import Iterable
from pathlib import Path

DEFAULT_SOURCE = Path(
    "~/Library/Mobile Documents/iCloud~net~dev2~healthsync/Documents/HealthSync"
).expanduser()
DEFAULT_INBOX = Path("/Volumes/nicolas-data/HealthData/healthsync-inbox")


def resolve_placeholder(name: str) -> str:
    """``.route-X.gpx.icloud`` → ``route-X.gpx``; any other name unchanged."""
    if name.startswith(".") and name.endswith(".icloud"):
        return name[1:-len(".icloud")]
    return name


def plan_copies(names: Iterable[str], present: set[str]) -> list[str]:
    """Names to copy: sidecars first, then deltas oldest-first.

    ``names`` is a raw directory listing (placeholders included); ``present``
    is what the inbox already holds.
    """
    fresh = {resolve_placeholder(n) for n in names} - present
    fresh = {n for n in fresh if not n.startswith(".")}
    sidecars = sorted(n for n in fresh if not n.startswith("delta-"))
    deltas = sorted(n for n in fresh if n.startswith("delta-"))
    return sidecars + deltas


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Mirror new HealthSync files from iCloud Drive into the inbox.")
    ap.add_argument("--inbox", type=Path, default=DEFAULT_INBOX)
    ap.add_argument("--source", type=Path, default=DEFAULT_SOURCE)
    args = ap.parse_args(argv)

    if not args.source.is_dir():
        print(f"iCloud folder not found: {args.source}", file=sys.stderr)
        return 1
    args.inbox.mkdir(parents=True, exist_ok=True)

    present = {p.name for p in args.inbox.iterdir() if not p.name.startswith(".")}
    for name in plan_copies((p.name for p in args.source.iterdir()), present):
        src = args.source / name
        try:
            # Reading the real path materialises an evicted file; the copy
            # blocks until iCloud has finished downloading it.
            shutil.copyfile(src, args.inbox / name)
        except (OSError, TimeoutError) as exc:
            print(f"not yet available, will retry: {name} ({exc})", file=sys.stderr)
            continue
        print(name)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
