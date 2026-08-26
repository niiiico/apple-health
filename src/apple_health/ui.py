"""Turn the store into the one page the interaction layer serves.

Kept apart from `web` for the reason tvledger's split exists: rendering that is
reachable only through a running HTTP server is rendering nobody tests. Every
function here is a pure function of already-fetched rows.
"""

from __future__ import annotations

import html
from datetime import date, timedelta
from pathlib import Path

TEMPLATE = Path(__file__).parent / "ui_template.html"


def _esc(value: object) -> str:
    """HTML-escape, rendering None as an empty string rather than 'None'."""
    return html.escape("" if value is None else str(value), quote=True)


def _num(value: object, places: int = 2, suffix: str = "") -> str:
    return "" if value is None else f"{float(value):.{places}f}{suffix}"


def coverage_line(coverage: dict) -> str:
    """The header every page carries: what is known, and to when.

    Stated on the page for the same reason the queries state it — a screen of
    sessions with no boundary reads as the whole story.
    """
    through = coverage.get("observed_through")
    if not through:
        return '<p class="cov warn">No ingest has run — the record is empty.</p>'
    return (f'<p class="cov">HealthKit observed through <b>{_esc(through[:16].replace("T", " "))}</b>'
            f" — anything after that is unknown, not absent.</p>")


def zone_models_section(models: list[dict]) -> str:
    """The dated zone timeline, plus the form to add to it.

    An empty timeline is called out rather than left blank: every zone number in
    the system is silently using built-in bands until something is recorded here.
    """
    if models and models[0].get("source") != "default":
        rows = "".join(
            f"<tr><td>{_esc(m['effective_from'])}</td><td>{_esc(m['source'])}</td>"
            f"<td>{_esc(m['boundaries']['z1'])}</td><td>{_esc(m['boundaries']['z2'])}</td>"
            f"<td>{_esc(m['boundaries']['z3'])}</td><td>{_esc(m['boundaries']['z4'])}</td>"
            f"<td>{_esc(m['boundaries']['z5'])}</td><td>{_esc(m.get('note'))}</td></tr>"
            for m in models)
        table = ("<table><tr><th>from</th><th>source</th><th>Z1</th><th>Z2</th>"
                 f"<th>Z3</th><th>Z4</th><th>Z5</th><th>note</th></tr>{rows}</table>")
    else:
        table = ('<p class="note warn">None recorded. Every zone figure in the system — '
                 "the Vault files, the session archive, the advisor — is using the "
                 "built-in bands, not what your watch actually showed. Add the model "
                 "in force and the ones before it.</p>")

    today = date.today().isoformat()
    return f"""<h2>Heart-rate zones</h2>
<div class="card">{table}</div>
<div class="card" data-card>
  <div class="grid">
    <label>effective from<input data-field="effective_from" type="date" value="{today}"></label>
    <label>source<select data-field="source">
      <option value="watch-auto">watch-auto</option><option value="manual">manual</option>
      <option value="lab">lab</option></select></label>
    <label>Z1 max<input data-field="z1_max" type="number" value="134"></label>
    <label>Z2 max<input data-field="z2_max" type="number" value="159"></label>
    <label>Z3 max<input data-field="z3_max" type="number" value="169"></label>
    <label>Z4 max<input data-field="z4_max" type="number" value="177"></label>
  </div>
  <input data-field="note" placeholder="why this changed — a watch recalculation, a lab test…">
  <button data-action="set_zone_model" data-reload="1">Record model</button>
  <span data-status></span>
</div>"""


def period_notes_section(notes: list[dict]) -> str:
    """Spans of context no sensor records — a closed pool, a trip, an injury."""
    if notes:
        cards = "".join(
            f'<div class="card"><div class="row"><span class="when">{_esc(n["from"])}</span>'
            f'<span class="stat">→ {_esc(n["to"] or "open")}</span></div>'
            f'<div>{_esc(n["note"])}</div></div>' for n in notes)
    else:
        cards = ('<p class="note">Nothing recorded. This is where "pool closed", '
                 '"travelling, no bike" or "ill" goes — without it the advisor '
                 "explains a drop in volume as lost fitness.</p>")
    return f"""<h2>Periods</h2>
{cards}
<div class="card" data-card>
  <div class="grid">
    <label>from<input data-field="starts_on" type="date"></label>
    <label>to (optional)<input data-field="ends_on" type="date"></label>
  </div>
  <textarea data-field="note" placeholder="piscine indisponible ; vélo de route trouvé tardivement…"></textarea>
  <button data-action="set_period_note" data-reload="1">Add period</button>
  <span data-status></span>
</div>"""


def window_nav(start: date, end: date, span: dict) -> str:
    """Move the window, and say what the whole record covers.

    The page opened on a fixed recent window and offered no way past it, so
    every session older than that was unreachable — you could not annotate the
    France block from September. The record's true extent is printed alongside
    so the window reads as a view, not as the extent of what exists.
    """
    days = (end - start).days + 1
    prev_end = start - timedelta(days=1)
    prev_start = prev_end - timedelta(days=days - 1)
    next_start = end + timedelta(days=1)
    next_end = next_start + timedelta(days=days - 1)
    earliest = (span.get("earliest") or "")[:10]
    latest = (span.get("latest") or "")[:10]
    return (
        f'<div class="card nav"><div class="row">'
        f'<a class="btn" href="/?from={prev_start}&to={prev_end}">&larr; earlier</a>'
        f'<form class="range" method="get" action="/">'
        f'<input type="date" name="from" value="{start}">'
        f'<input type="date" name="to" value="{end}">'
        f'<button type="submit">Show</button></form>'
        f'<a class="btn" href="/?from={next_start}&to={next_end}">later &rarr;</a>'
        f'</div><p class="note">Record runs {_esc(earliest)} to {_esc(latest)} '
        f'({span.get("workouts", 0):,} workouts) — any of it is reachable from here.</p></div>')


def sessions_section(sessions: list[dict]) -> str:
    """Sessions in the window, each with a place to say what the numbers cannot."""
    if not sessions:
        return ("<h2>Sessions</h2><p class=\"note\">None in this window — "
                "which is a fact about the window, not about the record.</p>")
    cards = []
    for s in sessions:
        stats = " · ".join(x for x in (
            _num(s.get("distance_km"), 2, " km") if s.get("distance_km") else "",
            _num(s.get("duration_min"), 0, " min") if s.get("duration_min") else "",
            f"FC {_num(s.get('avg_hr'), 0)}/{_num(s.get('max_hr'), 0)}" if s.get("avg_hr") else "",
        ) if x)
        flags = ""
        if not s.get("has_hr_series"):
            flags += '<span class="flag">no HR series</span>'
        if not s.get("has_laps"):
            flags += '<span class="flag">no laps</span>'
        cards.append(
            f'<div class="card" data-card>'
            f'<div class="row"><span class="when">{_esc(s["date"])}</span>'
            f'<span class="act">{_esc(s["activity"])}</span>'
            f'<span class="stat">{_esc(stats)}</span>{flags}'
            f'<a class="btn detail" href="/session/{s["id"]}">detail</a></div>'
            f'<input type="hidden" data-field="workout_id" value="{s["id"]}">'
            f'<textarea data-field="note" placeholder="how it went, what changed, why it was cut short…">'
            f'{_esc(s.get("note"))}</textarea>'
            f'<button data-action="set_session_note">Save note</button>'
            f'<span data-status></span></div>')
    return "<h2>Sessions</h2>" + "".join(cards)


def render(context: dict, sessions: list[dict], start: date, end: date) -> str:
    """The index page."""
    body = (
        "<h1>health</h1>"
        + coverage_line(context["coverage"])
        + window_nav(start, end, context["record"])
        + zone_models_section(context["zone_models"])
        + period_notes_section(context["period_notes"])
        + sessions_section(sessions)
        + "<footer>Everything on this page is what no sensor recorded.</footer>"
    )
    return TEMPLATE.read_text().replace("__BODY__", body)


def render_session(detail: dict) -> str:
    """One session in full — the zone data the store holds and the page hid.

    Durations as well as percentages: "62 % in Z2" and "44 minutes in Z2" answer
    different questions, and a training plan asks the second one.
    """
    if detail.get("error"):
        return TEMPLATE.read_text().replace(
            "__BODY__", f'<h1>health</h1><p class="note warn">{_esc(detail["error"])}</p>'
            '<p><a class="btn" href="/">back</a></p>')

    s, hr, zm = detail["session"], detail.get("hr"), detail["zone_model"]
    stats = " · ".join(x for x in (
        _num(s.get("distance_km"), 2, " km") if s.get("distance_km") else "",
        _num(s.get("duration_min"), 0, " min") if s.get("duration_min") else "",
        f"FC {_num(s.get('avg_hr'), 0)}/{_num(s.get('max_hr'), 0)}" if s.get("avg_hr") else "",
        _num(s.get("energy_kcal"), 0, " kcal") if s.get("energy_kcal") else "",
    ) if x)

    if hr:
        rows = "".join(
            f"<tr><td>{_esc(label)}</td>"
            f"<td>{int(hr['zone_seconds'][label]) // 60}:"
            f"{int(hr['zone_seconds'][label]) % 60:02d}</td>"
            f"<td>{hr['zone_percent'][label]:.0f} %</td></tr>"
            for label in hr["zone_percent"] if hr["zone_seconds"].get(label))
        drift = " → ".join(f"{d['avg']:.0f}" for d in hr["drift_thirds"]) or "—"
        hr_html = (
            f'<h2>Heart rate</h2><div class="card">'
            f'<p class="note">{hr["samples"]:,} samples · avg {hr["avg"]:.0f} · '
            f'{hr["min"]:.0f}–{hr["max"]:.0f} bpm</p>'
            f"<table><tr><th>zone</th><th>time</th><th>share</th></tr>{rows}</table>"
            f'<p class="note">Drift by thirds: {_esc(drift)}</p>'
            f'<p class="note">Zones from the <b>{_esc(zm["source"])}</b> model'
            + (f' effective {_esc(zm["effective_from"])}' if zm.get("effective_from") else "")
            + ".</p></div>")
    else:
        hr_html = ('<h2>Heart rate</h2><div class="card"><p class="note warn">'
                   "No series recorded for this session — avg and max only. That is "
                   "different from a flat one, and nothing here should be read as "
                   "zone distribution.</p></div>")

    laps = detail.get("laps")
    if laps:
        rows = "".join(f"<tr><td>{l['idx']}</td><td>{_num(l['duration_s'], 1, ' s')}</td>"
                       f"<td>{_num(l['distance_m'], 0, ' m')}</td></tr>" for l in laps)
        laps_html = ("<h2>Laps</h2><div class=\"card\"><table>"
                     f"<tr><th>#</th><th>time</th><th>distance</th></tr>{rows}</table></div>")
    else:
        laps_html = ('<h2>Laps</h2><div class="card"><p class="note">None recorded — '
                     "HealthSync does not export lap events yet, so splits still have "
                     "to be read off the watch.</p></div>")

    body = (
        f'<h1>{_esc(s["date"])} — {_esc(s["activity"])}</h1>'
        f'<p class="cov">{_esc(stats)} · started {_esc(s["started_at"][11:16])} '
        f'{_esc(s.get("tz") or "")}</p>'
        + coverage_line(detail["coverage"]) + hr_html + laps_html
        + f'<h2>Note</h2><div class="card" data-card>'
        f'<input type="hidden" data-field="workout_id" value="{s["id"]}">'
        f'<textarea data-field="note" placeholder="how it went, what changed, '
        f'why it was cut short…">{_esc(s.get("note"))}</textarea>'
        f'<button data-action="set_session_note">Save note</button>'
        f'<span data-status></span></div>'
        f'<p style="margin-top:2rem"><a class="btn" href="/">&larr; all sessions</a></p>'
    )
    return TEMPLATE.read_text().replace("__BODY__", body)
