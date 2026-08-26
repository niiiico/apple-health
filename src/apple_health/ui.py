"""Turn the store into the one page the interaction layer serves.

Kept apart from `web` for the reason tvledger's split exists: rendering that is
reachable only through a running HTTP server is rendering nobody tests. Every
function here is a pure function of already-fetched rows.
"""

from __future__ import annotations

import html
from datetime import date
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


def sessions_section(sessions: list[dict]) -> str:
    """Recent sessions, each with a place to say what the numbers cannot."""
    if not sessions:
        return "<h2>Sessions</h2><p class=\"note\">None in this window.</p>"
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
            f'<span class="stat">{_esc(stats)}</span>{flags}</div>'
            f'<input type="hidden" data-field="workout_id" value="{s["id"]}">'
            f'<textarea data-field="note" placeholder="how it went, what changed, why it was cut short…">'
            f'{_esc(s.get("note"))}</textarea>'
            f'<button data-action="set_session_note">Save note</button>'
            f'<span data-status></span></div>')
    return "<h2>Sessions</h2>" + "".join(cards)


def render(context: dict, sessions: list[dict], window_days: int) -> str:
    """The whole page."""
    body = (
        "<h1>health</h1>"
        + coverage_line(context["coverage"])
        + zone_models_section(context["zone_models"])
        + period_notes_section(context["period_notes"])
        + sessions_section(sessions)
        + f'<footer>Last {window_days} days · '
          f'{context["record"]["workouts"]:,} workouts on record · '
          "everything here is what no sensor recorded.</footer>"
    )
    return TEMPLATE.read_text().replace("__BODY__", body)
