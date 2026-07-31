"""Download new HealthSync files from Box into the local inbox (ADR-003).

The iOS app uploads delta JSON + sidecars to the ``HealthSync/`` folder at the
Box root. This tool mirrors anything the local inbox does not yet have, by
file *name* (deltas are immutable; ``hr-*.csv`` may be re-uploaded by the app's
backfill, so a size mismatch on an existing hr file re-downloads it).

Sidecars are downloaded before delta JSONs so a crash mid-fetch never leaves a
delta visible without its files — the same ordering rule the producer follows.

Usage::

    uv run python tools/box_fetch.py [--inbox PATH] [--folder-name HealthSync]

Prints one line per downloaded file; exits 0 with no output when up to date.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
from apple_health.box_client import BoxClient, BoxItem  # noqa: E402

DEFAULT_INBOX = Path("/Volumes/nicolas-data/HealthData/healthsync-inbox")


def plan_downloads(remote: list[BoxItem], local_names: set[str]) -> list[BoxItem]:
    """Remote files worth downloading, sidecars first then deltas ascending."""
    wanted = [i for i in remote if i.type == "file" and i.name not in local_names]
    sidecars = sorted((i for i in wanted if not i.name.startswith("delta-")),
                      key=lambda i: i.name)
    deltas = sorted((i for i in wanted if i.name.startswith("delta-")),
                    key=lambda i: i.name)
    return sidecars + deltas


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Mirror new HealthSync files from Box.")
    ap.add_argument("--inbox", type=Path, default=DEFAULT_INBOX)
    ap.add_argument("--folder-name", default="HealthSync")
    args = ap.parse_args(argv)

    args.inbox.mkdir(parents=True, exist_ok=True)
    box = BoxClient()
    folder_id = box.ensure_folder(args.folder_name)
    local = {p.name for p in args.inbox.iterdir() if p.is_file()}
    for item in plan_downloads(box.list_folder(folder_id), local):
        (args.inbox / item.name).write_bytes(box.download(item.id))
        print(f"fetched {item.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
