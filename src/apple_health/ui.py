"""Turn the store into the one page the interaction layer serves.

Kept apart from `web` for the reason tvledger's split exists: rendering that is
reachable only through a running HTTP server is rendering nobody tests. Every
function here is a pure function of already-fetched rows.
"""

from __future__ import annotations

import html
import math
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


def zone_bands_section(model: dict) -> str:
    """State the bands every zone figure on this page was computed with.

    Shown rather than assumed: "Z3 4:52" means nothing without them. One line,
    matching how the session files and Vault briefs print the same thing — the
    labels already carry their ranges, so a two-column table said it twice.
    """
    bands = " · ".join(_esc(label) for label in model["boundaries"])
    return f"""<h2>Heart-rate zones</h2>
<div class="card">
  <p>{bands}</p>
  <p class="note">{_esc(model["note"])}</p>
</div>"""


def chat_section(history: list[dict] | None = None) -> str:
    """A conversation box, above everything already said.

    History is stored and shown: an answer worth acting on is worth re-reading,
    and a question already asked is worth not asking twice. The model's *memory*
    of a thread still ends at a pod restart — the CLI keeps that in the pod —
    but the transcript outlives it, so what was said is never lost with it.
    """
    turns = ""
    for turn in (history or []):
        queries = turn.get("queries") or []
        note = (f'<div class="note">{len(queries)} requête(s) : '
                f'{_esc(", ".join(queries))}</div>' if queries else "")
        turns += (
            f'<div class="turn you">{_esc(turn["question"])}</div>'
            f'<div class="turn claude">{_esc(turn["answer"])}{note}'
            f'<div class="note">{_esc(turn["asked_at"][:16].replace("T", " "))}</div>'
            f"</div>")
    past = (f'<details class="card"><summary>Conversations précédentes '
            f'({len(history)})</summary><div class="transcript past">{turns}</div>'
            f"</details>" if history else "")

    return f"""<h2>Demander</h2>
<div class="card chat" data-card data-chat>
  <div data-transcript class="transcript"></div>
  <textarea data-field="message" rows="2"
    placeholder="par ex. « la nage de mardi, c'était correct ? »"></textarea>
  <button data-action="chat" data-chat-send data-slow>Envoyer</button>
  <span data-status></span>
</div>
{past}"""


def plan_section(plan: dict | None) -> str:
    """The standing plan, and the button that rewrites it."""
    if plan and plan.get("body"):
        body = (f'<div class="doc">{_esc(plan["body"])}</div>'
                f'<p class="note">Réécrit le {_esc(plan["updated_at"][:16])}.</p>')
    else:
        body = ('<p class="note">Aucun plan écrit pour l\'instant. Il est rédigé '
                "à partir des objectifs ci-dessus et de l'entraînement récent.</p>")
    return f"""<h2>Plan</h2>
<div class="card" data-card>
  {body}
  <button data-action="write_plan" data-reload="1" data-slow>Réécrire le plan</button>
  <span data-status></span>
</div>"""


def goals_section(goals: list[dict]) -> str:
    """What you are training for, and the form to say so.

    An empty list is called out rather than left blank: the advisor writes
    towards these, and with none recorded it is commenting rather than coaching.
    """
    if goals:
        rows = "".join(
            f"<tr><td>{_esc(g['goal'])}</td>"
            f"<td>{_esc(g['target_date'] or '—')}</td>"
            f'<td><button data-action="archive_goal" data-reload="1"'
            f" data-arg-id=\"{int(g['id'])}\">archive</button></td></tr>"
            for g in goals)
        table = ("<table><tr><th>goal</th><th>by</th><th></th></tr>"
                 f"{rows}</table>")
    else:
        table = ('<p class="note warn">None recorded. The advisor has nothing to '
                 "write towards, so it can describe your training but not judge "
                 "it against anything.</p>")

    return f"""<h2>Goals</h2>
<div class="card">{table}</div>
<div class="card" data-card>
  <input data-field="goal" placeholder="what you are training for, in your words">
  <label>target date (optional)<input data-field="target_date" type="date"></label>
  <button data-action="set_goal" data-reload="1">Record goal</button>
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
        + chat_section(context.get("chat_history"))
        + goals_section(context["goals"])
        + plan_section(context.get("plan"))
        + zone_bands_section(context["zone_model"])
        + period_notes_section(context["period_notes"])
        + sessions_section(sessions)
        + "<footer>Everything on this page is what no sensor recorded.</footer>"
    )
    return TEMPLATE.read_text().replace("__BODY__", body)


def route_section(route: dict | None) -> str:
    """The track and its elevation profile, drawn inline.

    SVG rather than a tile map on purpose: a tile layer would send the
    coordinates of every run past this house to whoever serves the tiles. The
    shape of the route is what a training review needs, and the shape costs
    nothing to draw and leaks nothing.
    """
    if not route or len(route.get("points") or []) < 2:
        return ""

    pts = route["points"]
    b = route["bounds"]
    lat_span = max(b["max_lat"] - b["min_lat"], 1e-6)
    lon_span = max(b["max_lon"] - b["min_lon"], 1e-6)
    # Longitude degrees shrink with latitude; without the cosine a Tokyo ride
    # comes out stretched by about a fifth east-to-west.
    mid_lat = math.radians((b["max_lat"] + b["min_lat"]) / 2)
    lon_span_m = lon_span * math.cos(mid_lat)
    scale = max(lat_span, lon_span_m)
    W = H = 100.0
    coords = " ".join(
        f"{(lon - b['min_lon']) * math.cos(mid_lat) / scale * W + (W - lon_span_m / scale * W) / 2:.2f},"
        # y is flipped: SVG counts downwards, latitude upwards.
        f"{H - ((lat - b['min_lat']) / scale * H + (H - lat_span / scale * H) / 2):.2f}"
        for lat, lon in pts)
    map_svg = (
        f'<svg viewBox="0 0 {W:.0f} {H:.0f}" class="route" '
        f'preserveAspectRatio="xMidYMid meet" role="img" aria-label="parcours">'
        f'<polyline points="{coords}" fill="none" stroke="currentColor" '
        f'stroke-width="1.2" stroke-linejoin="round" stroke-linecap="round"/>'
        f'<circle cx="{coords.split()[0].split(",")[0]}" '
        f'cy="{coords.split()[0].split(",")[1]}" r="1.8" fill="currentColor"/>'
        f"</svg>")

    profile = ""
    eles = route.get("elevation") or []
    lo, hi = route.get("ele_min"), route.get("ele_max")
    if lo is not None and hi is not None and hi - lo > 1:
        span = hi - lo
        n = len(eles)
        pairs = [(i / max(n - 1, 1) * 100.0, 30.0 - ((e - lo) / span * 28.0))
                 for i, e in enumerate(eles) if e is not None]
        if len(pairs) > 1:
            line = " ".join(f"{x:.2f},{y:.2f}" for x, y in pairs)
            area = f"0,30 {line} 100,30"
            profile = (
                f'<svg viewBox="0 0 100 30" class="profile" '
                f'preserveAspectRatio="none" role="img" aria-label="profil">'
                f'<polygon points="{area}" fill="currentColor" opacity="0.18"/>'
                f'<polyline points="{line}" fill="none" stroke="currentColor" '
                f'stroke-width="0.6"/></svg>'
                f'<p class="note">{lo:.0f}–{hi:.0f} m — profil échantillonné '
                f"sur {len(pairs)} points.</p>")

    return (f"<h2>Parcours</h2><div class=\"card\">{map_svg}{profile}"
            f'<p class="note">Tracé dessiné localement — aucune tuile, aucune '
            f"coordonnée ne quitte le réseau.</p></div>")


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
    review = detail.get("review")
    if review:
        review_html = (
            f'<div class="doc">{_esc(review["review"])}</div>'
            f'<p class="note">Écrite le {_esc(review["created_at"][:16])} par '
            f'{_esc(review["model"])}, sur un record couvert jusqu\'au '
            f'{_esc((review.get("observed_through") or "?")[:16])}.</p>')
    else:
        review_html = ('<p class="note">Pas encore analysée.</p>')
    review_section = (
        f'<h2>Analyse</h2><div class="card" data-card>{review_html}'
        f'<button data-action="review_session" data-reload="1" data-slow '
        f'data-arg-workout_id="{int(s["id"])}">'
        + ("Réanalyser" if review else "Analyser cette séance") +
        '</button> <span data-status></span></div>')

    # Conditions the watch recorded. Shown only when present: a blank means the
    # watch did not record it, and a printed 0 °C would read as a cold morning.
    conditions = " · ".join(x for x in (
        f"{s['weather_temp_c']:.0f} °C" if s.get("weather_temp_c") is not None else "",
        f"{s['weather_humidity_pct']:.0f} % hum."
        if s.get("weather_humidity_pct") is not None else "",
        f"D+ {s['elevation_ascended_m']:.0f} m"
        if s.get("elevation_ascended_m") is not None else "",
        f"D− {s['elevation_descended_m']:.0f} m"
        if s.get("elevation_descended_m") is not None else "",
        f"{s['avg_mets']:.1f} MET" if s.get("avg_mets") is not None else "",
        f"bassin {s['pool_length_m']:.0f} m"
        if s.get("pool_length_m") is not None else "",
        str(s.get("swim_location") or "").replace(
            "HKWorkoutSwimmingLocationType", ""),
    ) if x)

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
        + (f'<p class="cov">{_esc(conditions)}</p>' if conditions else "")
        + coverage_line(detail["coverage"]) + review_section
        + route_section(detail.get("route")) + hr_html + laps_html
        + f'<h2>Note</h2><div class="card" data-card>'
        f'<input type="hidden" data-field="workout_id" value="{s["id"]}">'
        f'<textarea data-field="note" placeholder="how it went, what changed, '
        f'why it was cut short…">{_esc(s.get("note"))}</textarea>'
        f'<button data-action="set_session_note">Save note</button>'
        f'<span data-status></span></div>'
        f'<p style="margin-top:2rem"><a class="btn" href="/">&larr; all sessions</a></p>'
    )
    return TEMPLATE.read_text().replace("__BODY__", body)
