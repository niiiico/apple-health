#!/bin/bash
# Render the four Vault files twice — once with the Python pipeline on the Mac,
# once with the app's own Swift renderers — and diff them.
#
# This is the ADR-005 side-by-side validation, runnable without a device: the
# renderers in ../VaultBrief/ are pure functions over value types, so they
# compile for the host and can be fed the same data health.db holds.
#
# Only the two provenance lines are expected to differ (the app says it rendered
# on-device from HealthKit, the Mac says it rendered from health.db). Any other
# diff is a real divergence between the two implementations.
#
#     ios/VaultBrief/Parity/check.sh [YYYY-MM-DD]
#
# The optional date pins "today", so the comparison is reproducible.

set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo="$(cd "$here/../../.." && pwd)"
src="$here/../VaultBrief"
work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT

today="${1:-$(date +%F)}"

echo "== rendering with Python (health.db) =="
uv run --directory "$repo" python "$here/dump_fixture.py" "$today" > "$work/fixture.json"
uv run --directory "$repo" python - "$today" "$work/py-out" <<'PY'
import sqlite3, sys
from datetime import date
from pathlib import Path
sys.path.insert(0, "tools")
import vault_push, vault_sport_week

today = date.fromisoformat(sys.argv[1])
out = Path(sys.argv[2]); out.mkdir(parents=True, exist_ok=True)
conn = sqlite3.connect("data/health.db")
for activity, (name, label) in vault_push.DISCIPLINES.items():
    (out / name).write_text(
        vault_push.render_discipline(conn, activity, label, vault_push.DEFAULT_INBOX, today))
(out / "sport-week-current.md").write_text(vault_sport_week.render(conn, today))
PY

echo "== rendering with Swift (app sources) =="
swiftc -O -o "$work/render" "$here/main.swift" "$src/Zones.swift" "$src/VaultRender.swift"
"$work/render" "$work/fixture.json" "$work/swift-out"

echo "== diff =="
status=0
for f in sport-natation-sessions.md sport-course-sessions.md sport-velo-sessions.md sport-week-current.md; do
    # Drop the provenance lines, which differ by design.
    filter='/^Généré par /d; /^_Snapshot generated /d'
    if diff <(sed "$filter" "$work/py-out/$f") <(sed "$filter" "$work/swift-out/$f"); then
        echo "  ok   $f"
    else
        echo "  DIFF $f"
        status=1
    fi
done
exit $status
