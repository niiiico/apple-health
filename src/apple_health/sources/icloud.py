"""Copy new HealthSync files from the iCloud Drive folder into the local inbox.

The iOS app writes delta JSON + sidecars into its ubiquity container
(``iCloud.net.dev2.healthsync/Documents/HealthSync``); the iCloud daemon syncs
them to this Mac. This step mirrors them into a durable inbox on the NAS —
iCloud evicts local copies of files it considers cold, and
``tools/session_detail.py`` needs the HR-series CSVs to stay readable long
after the delta itself was ingested.

**Eviction.** This container uses macOS *dataless* files: an evicted file keeps
its real directory entry (``stat`` reports the true size, ``SF_DATALESS`` set)
and reading it faults the data back in, blocking until the download finishes.
That is what materialises a file here. The legacy ``.<name>.icloud`` plist
representation is handled defensively by `resolve_placeholder` — no file in
this container has used it — but note that in *that* representation the real
name has no directory entry, so the copy raises `FileNotFoundError` and the
file is simply retried next cycle rather than downloaded.

Copies are ordered sidecars-first, then deltas oldest-first, and each file is
copied to a temp name and atomically renamed into place, so the inbox never
contains a truncated file or a delta whose sidecars are missing (the same
ordering rule the producer follows — see docs/delta-contract.md).

**Contract with `sync_cycle`:** prints one copied filename per line to stdout
and *nothing* to stdout when up to date; diagnostics go to stderr. Returns
non-zero on a hard failure (missing source folder, failed copy). Never add a
plain `print()` here — empty stdout is what tells `sync_cycle` there is
nothing new.

Usage::

    uv run ah-fetch [--inbox PATH] [--source PATH]
"""

from __future__ import annotations

import argparse
import os
import shutil
import sys
from collections.abc import Iterable
from pathlib import Path

DEFAULT_SOURCE = Path(
    "~/Library/Mobile Documents/iCloud~net~dev2~healthsync/Documents/HealthSync"
).expanduser()
DEFAULT_INBOX = Path("/Volumes/nicolas-data/HealthData/healthsync-inbox")


def resolve_placeholder(name: str) -> str:
    """``.route-X.gpx.icloud`` → ``route-X.gpx``; any other name unchanged.

    Returns the name unchanged when stripping would leave nothing, so a file
    literally called ``.icloud`` cannot resolve to the empty string (which
    would address the source directory itself).
    """
    if name.startswith(".") and name.endswith(".icloud"):
        return name[1:-len(".icloud")] or name
    return name


def plan_copies(names: Iterable[str], present: set[str]) -> list[str]:
    """Names to copy: sidecars first, then deltas oldest-first.

    ``names`` is a listing of regular files (placeholders included);
    ``present`` is what the inbox already holds.
    """
    fresh = {resolve_placeholder(n) for n in names} - present
    fresh = {n for n in fresh if n and not n.startswith(".")}
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

    present = {p.name for p in args.inbox.iterdir() if p.is_file()}
    names = [p.name for p in args.source.iterdir() if p.is_file()]
    failed = 0
    for name in plan_copies(names, present):
        src = args.source / name
        # Copy to a dot-prefixed temp (excluded from `present` and from any
        # future plan) and rename only once whole: an interrupted copy must
        # never leave a truncated file that later runs would treat as done.
        tmp = args.inbox / f".{name}.part"
        try:
            # Reading a dataless file materialises it; this blocks until
            # iCloud has finished downloading.
            shutil.copyfile(src, tmp)
            os.replace(tmp, args.inbox / name)
        except FileNotFoundError:
            # Legacy .icloud placeholder: no real directory entry to read.
            tmp.unlink(missing_ok=True)
            print(f"not materialised, will retry: {name}", file=sys.stderr)
            continue
        except OSError as exc:
            tmp.unlink(missing_ok=True)
            print(f"copy failed: {name} ({exc})", file=sys.stderr)
            failed += 1
            continue
        print(name)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
