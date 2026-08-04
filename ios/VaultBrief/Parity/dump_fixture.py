"""Dump the exact inputs the Vault renderers consume, as JSON for the Swift harness."""
import csv, json, sqlite3, sys
from datetime import date, datetime, timedelta
from pathlib import Path

REPO = Path("/Volumes/nicolas-data/Repositories/apple-health")
sys.path.insert(0, str(REPO / "tools"))
INBOX = Path("/Volumes/nicolas-data/HealthData/healthsync-inbox")
TODAY = date.fromisoformat(sys.argv[1]) if len(sys.argv) > 1 else date.today()

conn = sqlite3.connect(REPO / "data/health.db")
conn.row_factory = sqlite3.Row


def epoch(s):
    """DB 'start' is an Apple Health date string with offset."""
    return datetime.strptime(s, "%Y-%m-%d %H:%M:%S %z").timestamp()


def sess(r):
    return {
        "uuid": r["uuid"] or f"none-{r['id']}",
        "activity": r["activity"],
        "start": epoch(r["start"]),
        "startDay": r["start"][:10],
        "durationMin": r["duration_min"],
        "distanceKm": r["distance_km"],
        "energyKcal": r["energy_kcal"],
        "avgHR": r["avg_hr"],
        "maxHR": r["max_hr"],
    }


def series(uuid):
    p = INBOX / f"hr-{uuid}.csv"
    if not p.exists():
        return None
    out = []
    with open(p) as f:
        for row in csv.DictReader(f):
            t = datetime.fromisoformat(row["time"].replace("Z", "+00:00"))
            out.append([t.timestamp(), float(row["bpm"])])
    return out


out = {"today": TODAY.isoformat(), "disciplines": {}, "series": {}}

for activity, label in [("Swimming", "natation"), ("Running", "course"), ("Cycling", "vélo")]:
    rows = conn.execute(
        "SELECT * FROM workouts WHERE activity=? ORDER BY start DESC LIMIT 5", (activity,)
    ).fetchall()
    out["disciplines"][activity] = {"label": label, "sessions": [sess(r) for r in rows]}
    for r in rows:
        if r["uuid"] and (s := series(r["uuid"])):
            out["series"][r["uuid"]] = s

monday = TODAY - timedelta(days=TODAY.weekday())
tomorrow = TODAY + timedelta(days=1)
four = monday - timedelta(weeks=4)
look = TODAY - timedelta(weeks=6)


def window(a, b, desc=False):
    q = f"SELECT * FROM workouts WHERE start >= ? AND start < ? ORDER BY start {'DESC' if desc else ''}"
    return [sess(r) for r in conn.execute(q, (a.isoformat(), b.isoformat())).fetchall()]


def avg(t, a, b):
    return conn.execute(
        "SELECT avg(value) FROM records WHERE type=? AND start >= ? AND start < ?",
        (t, a.isoformat(), b.isoformat()),
    ).fetchone()[0]


out["week"] = window(monday, tomorrow)
out["prior"] = window(four, monday)
out["recent"] = window(look, monday, desc=True)
out["monday"] = monday.isoformat()
out["restingHR"] = [avg("RestingHeartRate", monday, tomorrow), avg("RestingHeartRate", four, monday)]
out["hrv"] = [avg("HeartRateVariabilitySDNN", monday, tomorrow),
              avg("HeartRateVariabilitySDNN", four, monday)]

print(json.dumps(out))
