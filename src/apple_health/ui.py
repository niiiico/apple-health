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

from . import tiles

TEMPLATE = Path(__file__).parent / "ui_template.html"

# HealthKit enum -> what he calls it. "HighIntensityIntervalTraining" is 29
# unbreakable characters in a 311px row; the rest were simply English.
ACTIVITIES = {
    "Running": "Course", "Swimming": "Natation", "Cycling": "Velo",
    "Walking": "Marche", "Hiking": "Randonnee", "Climbing": "Escalade",
    "SwimBikeRun": "Triathlon", "HighIntensityIntervalTraining": "Fractionne",
    "TraditionalStrengthTraining": "Renfo",
    "FunctionalStrengthTraining": "Renfo", "CoreTraining": "Gainage",
    "Rowing": "Rameur", "Elliptical": "Elliptique",
    "StairClimbing": "Escaliers", "Cooldown": "Retour au calme",
}


def _activity(name):
    """Display name, falling back to the raw enum rather than hiding it."""
    return ACTIVITIES.get(name or "", name or "-")


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
    return f"""<h2>Zones FC</h2>
<div class="card">
  <p>{bands}</p>
  <p class="note">{_esc(model["note"])}</p>
</div>"""


def chat_section(history: list[dict] | None = None) -> str:
    """A conversation box, above everything already said.

    The last exchange is shown, not just linked. This docstring used to claim
    history was displayed while the transcript div was rendered empty every
    time: he would ask something, read the answer, lock the phone, and find a
    blank box the next morning with the answer two taps away.

    One turn, not the thread. This is the landing page; the conversation itself
    lives at /chat, and burying the sessions under a transcript would repeat the
    mistake this reordering just fixed.
    """
    last = ""
    if history:
        turn = history[0]
        last = (f'<div class="turn you">{_esc(turn["question"])}</div>'
                f'<div class="turn claude">{_esc(turn["answer"])}</div>')
    past = ('<p class="note"><a href="/chat">Toutes les discussions →</a></p>'
            if history else "")

    return f"""<h2>Demander</h2>
<div class="card chat" data-card data-chat>
  <div data-transcript class="transcript last">{last}</div>
  <textarea data-field="message" rows="2"
    placeholder="par ex. « la nage de mardi, c'était correct ? »"></textarea>
  <button data-action="chat" data-chat-send data-slow>Envoyer</button>
  <span data-status></span>
</div>
{past}"""


def plan_section(plan: dict | None) -> str:
    """The plan, whichever document is the plan.

    This read only the advisor's own slug and so announced "no plan" while the
    database held a six-thousand-word race plan under its own name, reachable
    from no page in the app.
    """
    if plan and plan.get("body"):
        versions = (f'<a class="note" href="/versions/documents/{_esc(plan["slug"])}">'
                    f'{plan.get("versions", 0)} version(s) précédente(s) →</a>'
                    if plan.get("versions") else "")
        body = (f'<details class="card"><summary>{_esc(plan["slug"])} — '
                f'{_esc(plan["updated_at"][:10])}</summary>'
                f'<div class="doc">{_esc(plan["body"])}</div>'
                # Editable here rather than only by the advisor or a script:
                # the document the whole block is organised around should not
                # need either to change a line.
                f'<div data-card><input type="hidden" data-field="slug" '
                f'value="{_esc(plan["slug"])}">'
                f'<textarea data-field="body" rows="10">{_esc(plan["body"])}</textarea>'
                f'<button data-action="set_document" data-reload="1" '
                f'data-confirm="Remplacer ce document ? La version actuelle sera '
                f'conservée.">Enregistrer</button> {versions}'
                f'<span data-status></span></div></details>')
        label = "Reecrire le plan"
    else:
        body = ('<p class="note">Aucun plan. Il sera redige a partir des '
                "objectifs et de l'entrainement recent.</p>")
        # "Rewrite" over an empty state offers to redo something that does not
        # exist.
        label = "Ecrire le plan"
    return f"""<h2>Plan</h2>
{body}
<div class="card" data-card>
  <button data-action="write_plan" data-reload="1" data-slow>{label}</button>
  <span data-status></span>
</div>"""


def _turn_html(turn: dict, editable: bool = False) -> str:
    queries = turn.get("queries") or []
    note = (f'<div class="note">{len(queries)} requête(s) : '
            f'{_esc(", ".join(queries))}</div>' if queries else "")
    when = _esc(turn["asked_at"][:16].replace("T", " "))
    edit = ""
    if editable and turn.get("id"):
        # The question travels in a data attribute so the box can be filled
        # without a round trip, and the button says what it will destroy.
        edit = (f'<button class="link" data-edit="{int(turn["id"])}" '
                f'data-question="{_esc(turn["question"])}">réécrire</button>')
    return (f'<div class="turn you">{_esc(turn["question"])}{edit}</div>'
            f'<div class="turn claude">{_esc(turn["answer"])}{note}'
            f'<div class="note">{when}</div></div>')


def render_chats(sessions: list[dict]) -> str:
    """The conversation list: one row per thread, newest first."""
    if sessions:
        rows = "".join(
            f'<a class="chatrow" href="/chat/{_esc(s["session_id"])}">'
            f'<span class="q">{_esc(s["first_question"])}</span>'
            f'<span class="note">{_esc(s["last_at"][:16].replace("T", " "))} · '
            f'{s["turns"]} échange(s)</span></a>'
            for s in sessions)
    else:
        rows = ('<p class="note">Aucune conversation. Pose une question depuis '
                "la page d'accueil ou ci-dessous.</p>")
    body = (
        "<h1>Discussions</h1>"
        f'<p><a class="btn" href="/chat/new">Nouvelle conversation</a> '
        f'<a class="btn" href="/">← séances</a></p>'
        f'<div class="card">{rows}</div>')
    return TEMPLATE.read_text().replace("__BODY__", body)


def render_chat(session_id: str | None, turns: list[dict]) -> str:
    """One conversation, full screen, ready to continue.

    `session_id` is carried on the form rather than in a hidden field the user
    could not see: continuing a thread is the whole point of the page, and the
    id is what makes the CLI pick up where it left off — or, if the pod has
    restarted since, what finds the transcript to rebuild it from.
    """
    transcript = "".join(_turn_html(t, editable=True) for t in turns)
    title = _esc(turns[0]["question"][:70]) if turns else "Nouvelle conversation"
    body = (
        f"<h1>{title}</h1>"
        f'<p><a class="btn" href="/chat">← discussions</a></p>'
        f'<div class="transcript full" data-transcript>{transcript}</div>'
        f'<div class="card chat" data-card data-chat'
        + (f' data-session="{_esc(session_id)}"' if session_id else "")
        + '>'
        '<input type="hidden" data-field="turn_id" value="">'
        '<textarea data-field="message" rows="3" autofocus '
        'placeholder="pose ta question…"></textarea>'
        '<button data-action="chat" data-chat-send data-slow>Envoyer</button>'
        '<button data-action="retry_turn" data-retry data-slow hidden>'
        'Réécrire et relancer</button>'
        '<button class="link" data-cancel-edit hidden>annuler</button>'
        '<span data-status></span></div>')
    return TEMPLATE.read_text().replace("__BODY__", body)


def goals_section(goals: list[dict]) -> str:
    """What he is training for, one card each.

    Not a table. These are 300-500 characters of prose, and a table cell beside
    a date column and a button left about 150px for them on a phone — twenty-five
    wrapped lines per goal, all of it above the sessions.
    """
    if goals:
        cards = ""
        for g in goals:
            when = (f'<span class="note">echeance {_esc(g["target_date"])}</span>'
                    if g.get("target_date") else "")
            versions = (f'<a class="note" href="/versions/goals/{int(g["id"])}">'
                        f'{g["versions"]} version(s) précédente(s) →</a>'
                        if g.get("versions") else "")
            cards += (
                f'<div class="card" data-card>'
                # Editable in place. A goal was add-only, so changing a word
                # meant archiving one and writing another.
                f'<textarea data-field="goal" rows="3">{_esc(g["goal"])}</textarea>'
                f'<label>echeance<input data-field="target_date" type="date" '
                f'value="{_esc(g.get("target_date") or "")}"></label>'
                f'<input type="hidden" data-field="id" value="{int(g["id"])}">'
                f'<button data-action="set_goal" data-reload="1">Enregistrer</button> '
                f'{when} {versions} '
                # Quiet, and it asks first. It was the loudest control in the
                # section, unconfirmed, on a cramped touch row — one fat-finger
                # tap permanently archived the race goal, with no undo anywhere.
                f'<button class="ghost" data-action="archive_goal" data-reload="1" '
                f'data-arg-id="{int(g["id"])}" '
                f'data-confirm="Archiver cet objectif ?">archiver</button>'
                f"<span data-status></span></div>")
    else:
        cards = ('<p class="note warn">Aucun objectif. L\'assistant n\'a rien vers '
                 "quoi ecrire : il peut decrire ton entrainement, pas le juger.</p>")

    return f"""<h2>Objectifs</h2>{cards}
<div class="card" data-card>
  <input data-field="goal" placeholder="ce que tu prepares, dans tes mots">
  <label>echeance (optionnel)<input data-field="target_date" type="date"></label>
  <button data-action="set_goal" data-reload="1">Enregistrer</button>
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
    return f"""<h2>Périodes</h2>
{cards}
<div class="card" data-card>
  <div class="grid">
    <label>du<input data-field="starts_on" type="date"></label>
    <label>au (optionnel)<input data-field="ends_on" type="date"></label>
  </div>
  <textarea data-field="note" placeholder="piscine indisponible ; vélo de route trouvé tardivement…"></textarea>
  <button data-action="set_period_note" data-reload="1">Ajouter</button>
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
    # The default window ends today, so "plus recent" pointed at a window
    # entirely in the future and always answered "aucune dans cette fenetre" —
    # a control that was wrong every time it was reachable.
    has_later = end < date.today()
    earliest = (span.get("earliest") or "")[:10]
    latest = (span.get("latest") or "")[:10]
    later = (f'<a class="btn" href="/?from={next_start}&to={next_end}">'
             f"plus récent &rarr;</a>") if has_later else ""
    return (
        f'<div class="card nav"><div class="row">'
        f'<a class="btn" href="/?from={prev_start}&to={prev_end}">&larr; plus ancien</a>'
        f'<form class="range" method="get" action="/">'
        f'<input type="date" name="from" value="{start}">'
        f'<input type="date" name="to" value="{end}">'
        f'<button type="submit">Afficher</button></form>'
        + later
        + f'</div><p class="note">Le dossier va du {_esc(earliest)} au {_esc(latest)} '
          f'({span.get("workouts", 0):,} séances) — tout est atteignable d\'ici.'
          f"</p></div>")


def sessions_section(sessions: list[dict]) -> str:
    """Sessions in the window, each with a place to say what the numbers cannot."""
    if not sessions:
        return ("<h2>Séances</h2><p class=\"note\">Aucune dans cette fenêtre — "
                "ce qui dit quelque chose de la fenêtre, pas du dossier.</p>")
    cards = []
    for s in sessions:
        stats = " · ".join(x for x in (
            _num(s.get("distance_km"), 2, " km") if s.get("distance_km") else "",
            _num(s.get("duration_min"), 0, " min") if s.get("duration_min") else "",
            f"FC moy {_num(s.get('avg_hr'), 0)} - max {_num(s.get('max_hr'), 0)}"
            if s.get("avg_hr") else "",
        ) if x)
        # Nothing is flagged for being absent any more. "no laps" fired on 19
        # of 22 rows and only ever restated the activity beside it -- no run or
        # ride in thirteen years has laps. "no HR series" fires on none of the
        # recent window and on 98.8% of the whole record, so paging backwards
        # changed which pill decorated every row: it described the ingest era,
        # not the session. A flag that fires on nearly everything is skipped.
        flags = ""
        if s.get("has_laps"):
            flags += '<span class="flag">longueurs</span>'
        cards.append(
            f'<div class="card" data-card>'
            f'<div class="row"><span class="when">{_esc(s["date"])}</span>'
            f'<span class="act">{_esc(_activity(s["activity"]))}</span>'
            f'<span class="stat">{_esc(stats)}</span>{flags}'
            f'<a class="btn detail" href="/session/{s["id"]}">détail</a></div>'
            # Folded away unless there is something to read. Nineteen of
            # twenty-two sessions had no note, and an always-open textarea plus
            # its button is ~95px each — about 45% of the list's height given to
            # a control used twice a week.
            f'<details class="notebox"{" open" if s.get("note") else ""}>'
            f'<summary>{"note" if s.get("note") else "+ note"}</summary>'
            f'<input type="hidden" data-field="workout_id" value="{s["id"]}">'
            f'<textarea data-field="note" rows="3" placeholder="comment ça s\'est '
            f'passé, ce qui a changé, pourquoi c\'était écourté…">'
            f'{_esc(s.get("note"))}</textarea>'
            f'<button data-action="set_session_note">Enregistrer</button>'
            f'<span data-status></span></details></div>')
    return "<h2>Séances</h2>" + "".join(cards)


def note_history_section(history: list[dict] | None) -> str:
    """Superseded versions of the note, folded away.

    Shown at all because a note is the only thing on a session no sensor can
    reproduce, and until now every edit destroyed the previous text — the
    athlete's own form as readily as the advisor's tool.
    """
    if not history:
        return ""
    rows = "".join(
        f'<div class="turn claude">{_esc(h["note"])}'
        f'<div class="note">remplacée le '
        f'{_esc(h["archived_at"][:16].replace("T", " "))} '
        f'({"toi" if h["replaced_by"] == "athlete" else "l\'assistant"})</div></div>'
        for h in history)
    return (f'<details class="card"><summary>Versions précédentes '
            f"({len(history)})</summary>{rows}</details>")


def render_versions(target: str, key: str, versions: list[dict],
                    label: str = "") -> str:
    """Superseded versions of one thing, newest first."""
    rows = "".join(
        f'<div class="card"><div class="doc">{_esc(v["body"])}</div>'
        f'<p class="note">remplacée le '
        f'{_esc(v["archived_at"][:16].replace("T", " "))} '
        f'({"toi" if v["replaced_by"] == "athlete" else "l\'assistant"})</p></div>'
        for v in versions)
    if not rows:
        rows = '<p class="note">Aucune version précédente.</p>'
    return TEMPLATE.read_text().replace("__BODY__", (
        f"<h1>Versions — {_esc(label or key)}</h1>"
        f'<p><a class="btn" href="/">&larr; retour</a></p>{rows}'))


def render_error(exc: Exception) -> str:
    """A readable failure page.

    The class name is shown and the message is not: a psycopg error carries the
    DSN, which names the host, the database and the user. That belongs in the
    pod's log, where it already is, and not on a screen.
    """
    body = ('<h1>Ça a cassé</h1>'
            '<p class="cov">La page n\'a pas pu être rendue. Le détail est dans '
            "le journal du serveur ; rien n'a été modifié.</p>"
            f'<div class="card"><p class="note warn">{_esc(type(exc).__name__)}</p></div>'
            '<p><a class="btn" href="/">← réessayer</a></p>')
    return TEMPLATE.read_text().replace("__BODY__", body)


def headline(goals: list[dict] | None) -> str:
    """The title, carrying the countdown when a dated goal exists.

    "health" earned a whole line and said nothing. Days-to-race is the most
    decision-relevant number he has and was buried mid-paragraph in a cell.
    """
    # The furthest dated goal, not the nearest. Earlier deadlines are milestones
    # on the way to it — taking the soonest put "Livrable S13" in the headline
    # and left the race it is a milestone for out of the page entirely.
    target = None
    for g in (goals or []):
        if not g.get("target_date"):
            continue
        try:
            when = date.fromisoformat(g["target_date"])
        except ValueError:
            continue
        if when >= date.today() and (target is None or when > target[0]):
            target = (when, g["goal"])
    if target is None:
        return "<h1>health</h1>"
    days = (target[0] - date.today()).days
    # First clause only — the goal itself is a paragraph — cut on a word.
    label = target[1].split(".")[0].split(",")[0].split(" (")[0]
    if len(label) > 40:
        label = label[:40].rsplit(" ", 1)[0] + "…"
    return (f'<h1>J-{days} <span class="muted">— {_esc(label)}, '
            f'{_esc(target[0].strftime("%d/%m"))}</span></h1>')


def window_summary(sessions: list[dict], start: date, end: date,
                   record: dict) -> str:
    """What this window actually held, so it need not be counted by eye."""
    if not sessions:
        return ""
    minutes = sum(s.get("duration_min") or 0 for s in sessions)
    by_sport: dict[str, int] = {}
    for s in sessions:
        name = _activity(s.get("activity"))
        by_sport[name] = by_sport.get(name, 0) + 1
    split = " · ".join(f"{n} {name.lower()}"
                       for name, n in sorted(by_sport.items(), key=lambda kv: -kv[1]))
    hours = f"{int(minutes // 60)} h {int(minutes % 60):02d}"
    return (f'<p class="cov"><b>{len(sessions)} seances</b> · {hours} · '
            f"{_esc(split)}</p>")


def render(context: dict, sessions: list[dict], start: date, end: date) -> str:
    """The index page."""
    # Order follows use. Sessions used to start about 1,800px down on a phone —
    # two full screens past goals, plan, zones and periods, which are settings
    # and reference. Those are write forms and a static line; the record is what
    # the page is for.
    body = (
        headline(context.get("goals"))
        + coverage_line(context["coverage"])
        + window_summary(sessions, start, end, context["record"])
        + chat_section(context.get("chat_history"))
        + sessions_section(sessions)
        + window_nav(start, end, context["record"])
        + plan_section(context.get("plan"))
        + goals_section(context["goals"])
        + period_notes_section(context["period_notes"])
        + zone_bands_section(context["zone_model"])
        + "<footer>Ce que la montre n'enregistre pas se note ici.</footer>"
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
    # Tiles under the track, in one shared projection. The polyline below is
    # kept in its own equirectangular box as a fallback; this block replaces it
    # when a grid can be computed, because two projections would put the track
    # in the field beside the road.
    grid = tiles.layout(b)
    proj = tiles.project(pts, grid)
    imgs = "".join(
        f'<img src="/tiles/{grid["z"]}/{grid["x0"] + c}/{grid["y0"] + r}.png" '
        f'loading="lazy" alt="" width="256" height="256" '
        f'style="left:{c * 256}px;top:{r * 256}px">'
        for r in range(grid["rows"]) for c in range(grid["cols"]))
    track = " ".join(f"{x:.1f},{y:.1f}" for x, y in proj)
    mapped = (
        f'<div class="mapwrap" style="aspect-ratio:{grid["width"]}/{grid["height"]}">'
        f'<div class="tiles" style="width:{grid["width"]}px;height:{grid["height"]}px">'
        f"{imgs}"
        f'<svg viewBox="0 0 {grid["width"]} {grid["height"]}" class="track" '
        f'preserveAspectRatio="none">'
        f'<polyline points="{track}" fill="none" stroke="#e8590c" '
        f'stroke-width="4" stroke-linejoin="round" stroke-linecap="round" '
        f'opacity="0.9"/></svg></div></div>'
        f'<p class="note">Fond de carte © OpenStreetMap. Les tuiles sont '
        f"récupérées par le serveur, jamais par ce navigateur, et gardées en "
        f"cache — une tuile n'est demandée qu'une seule fois.</p>")

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

    return (f"<h2>Parcours</h2><div class=\"card\">{mapped}{profile}"
            f"</div>")


def segments_section(segments: list[dict] | None, events: list[dict] | None) -> str:
    """The legs of a workout, and the markers the watch left in it.

    Segments are what a structured session actually was — intervals, or the legs
    of a triathlon. The export names no activity per segment, so a leg shows the
    workout's own; that is stated rather than dressed up, because inventing
    "Swimming" for leg one of a triathlon would be a fact nobody recorded.
    """
    out = ""
    if segments:
        rows = ""
        for s in segments:
            dur = (f"{int(s['duration_s'] // 60)}:{int(s['duration_s'] % 60):02d}"
                   if s.get("duration_s") else "—")
            stats = s.get("stats") or {}
            hr = stats.get("HeartRate") or {}
            extra = f"{hr['avg']:.0f} bpm" if hr.get("avg") else ""
            rows += (f"<tr><td>{s['idx']}</td>"
                     f"<td>{_esc(s['started_at'][11:19])}</td>"
                     f"<td>{_esc(dur)}</td><td>{_esc(extra)}</td>"
                     f"<td>{len(stats)} mesure(s)</td></tr>")
        out += ("<h2>Segments</h2><div class=\"card\"><table>"
                "<tr><th>#</th><th>début</th><th>durée</th><th>FC moy</th>"
                f"<th>données</th></tr>{rows}</table>"
                '<p class="note">Découpage enregistré par la montre. '
                "L'export ne nomme pas le sport de chaque segment.</p></div>")
    if events:
        chips = " · ".join(f"{e['count']}× {_esc(e['kind'])}" for e in events)
        out += (f'<h2>Marqueurs</h2><div class="card"><p>{chips}</p>'
                '<p class="note">Laps, segments, pauses et reprises tels que '
                "la montre les a posés.</p></div>")
    return out


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
        f"FC moy {_num(s.get('avg_hr'), 0)} - max {_num(s.get('max_hr'), 0)}"
            if s.get("avg_hr") else "",
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
            f'<h2>Fréquence cardiaque</h2><div class="card">'
            f'<p class="note">{hr["samples"]:,} samples · avg {hr["avg"]:.0f} · '
            f'{hr["min"]:.0f}–{hr["max"]:.0f} bpm</p>'
            f"<table><tr><th>zone</th><th>time</th><th>share</th></tr>{rows}</table>"
            f'<p class="note">Drift by thirds: {_esc(drift)}</p>'
            f'<p class="note">Zones from the <b>{_esc(zm["source"])}</b> model'
            + (f' effective {_esc(zm["effective_from"])}' if zm.get("effective_from") else "")
            + ".</p></div>")
    else:
        hr_html = ('<h2>Fréquence cardiaque</h2><div class="card"><p class="note warn">'
                   "No series recorded for this session — avg and max only. That is "
                   "different from a flat one, and nothing here should be read as "
                   "zone distribution.</p></div>")

    laps = detail.get("laps")
    if laps:
        rows = "".join(f"<tr><td>{l['idx']}</td><td>{_num(l['duration_s'], 1, ' s')}</td>"
                       f"<td>{_num(l['distance_m'], 0, ' m')}</td></tr>" for l in laps)
        swim = detail.get("swim") or {}
        best = ""
        for metres in (100, 200, 400):
            w = swim.get(f"best_{metres}m")
            if not w:
                continue
            secs = w["elapsed_s"]
            mark = "continu" if w["continuous"] else f"dont {w['rest_s']:.0f} s de repos"
            best += (f"<tr><td>{metres} m</td>"
                     f"<td>{int(secs // 60)}:{secs % 60:04.1f}</td>"
                     f"<td>{int(w['per_100m_s'] // 60)}:{w['per_100m_s'] % 60:04.1f}"
                     f"/100m</td><td>{mark}</td></tr>")
        summary = (f'<table><tr><th>distance</th><th>temps</th><th>allure</th>'
                   f"<th></th></tr>{best}</table>"
                   f'<p class="note">Fenêtres mesurées bord à bord, repos compris.'
                   "</p>") if best else ""
        laps_html = (f"<h2>Longueurs</h2><div class=\"card\">{summary}"
                     "<details><summary>{} longueur(s)</summary><table>".format(len(laps))
                     + f"<tr><th>#</th><th>temps</th><th>distance</th></tr>{rows}"
                     "</table></details></div>")
    else:
        # No heading at all. A run has no laps and never will, so "Laps — none
        # recorded" was a section rendered on nearly every page to say nothing.
        laps_html = ""

    body = (
        f'<h1>{_esc(s["date"])} — {_esc(_activity(s["activity"]))}</h1>'
        f'<p class="cov">{_esc(stats)} · départ {_esc(s["started_at"][11:16])} '
        f'{_esc(s.get("tz") or "")}</p>'
        + (f'<p class="cov">{_esc(conditions)}</p>' if conditions else "")
        + coverage_line(detail["coverage"]) + review_section
        + route_section(detail.get("route"))
        + segments_section(detail.get("segments"), detail.get("events"))
        + hr_html + laps_html
        + f'<h2>Note</h2><div class="card" data-card>'
        f'<input type="hidden" data-field="workout_id" value="{s["id"]}">'
        f'<textarea data-field="note" rows="3" placeholder="comment ça s\'est passé, '
        f'ce qui a changé, pourquoi c\'était écourté…">{_esc(s.get("note"))}</textarea>'
        f'<button data-action="set_session_note">Enregistrer</button>'
        f'<span data-status></span></div>'
        + note_history_section(detail.get("note_history"))
        + f'<p style="margin-top:2rem"><a class="btn" href="/">&larr; toutes les séances</a></p>'
    )
    return TEMPLATE.read_text().replace("__BODY__", body)
