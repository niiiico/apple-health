"""Turn the store into the one page the interaction layer serves.

Kept apart from `web` for the reason tvledger's split exists: rendering that is
reachable only through a running HTTP server is rendering nobody tests. Every
function here is a pure function of already-fetched rows.
"""

from __future__ import annotations

import html
import json
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


def _page(body: str, here: str = "") -> str:
    """Wrap a body in the shell, marking which nav entry is current."""
    html = TEMPLATE.read_text().replace("__BODY__", body)
    if here:
        html = html.replace(f'<a href="{here}">', f'<a href="{here}" aria-current="page">')
    return html


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


def render_chats(sessions: list[dict], archived: bool = False) -> str:
    """The conversation list: one row per thread, newest first.

    Archiving is per row and does not confirm: it hides, and the archive is one
    link away. A confirmation for something reversible is a tax on the common
    case — unlike rewriting a question, which destroys the answers after it.
    """
    if sessions:
        rows = ""
        for s in sessions:
            label = "restaurer" if archived else "archiver"
            rows += (
                f'<div class="chatrow">'
                f'<a href="/chat/{_esc(s["session_id"])}">'
                f'<span class="q">{_esc(s["first_question"])}</span>'
                f'<span class="note">{_esc(s["last_at"][:16].replace("T", " "))} · '
                f'{s["turns"]} échange(s)</span></a>'
                f'<span data-card><input type="hidden" data-field="session_id" '
                f'value="{_esc(s["session_id"])}">'
                + (f'<input type="hidden" data-field="restore" value="1">'
                   if archived else "")
                + f'<button class="ghost" data-action="archive_chat" '
                  f'data-reload="1">{label}</button>'
                  f"<span data-status></span></span></div>")
    elif archived:
        rows = '<p class="note">Aucune conversation archivée.</p>'
    else:
        rows = ('<p class="note">Aucune conversation. Pose une question depuis '
                "la page d'accueil ou ci-dessous.</p>")
    other = ('<a class="btn" href="/chat">← discussions</a>' if archived
             else '<a class="btn" href="/chat?archived=1">Archivées</a>')
    body = (
        f"<h1>{'Discussions archivées' if archived else 'Discussions'}</h1>"
        f'<p><a class="btn" href="/chat/new">Nouvelle conversation</a> '
        f'{other} <a class="btn" href="/">← séances</a></p>'
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
        f'<p><a class="btn" href="/chat">← discussions</a>'
        + (f'<span data-card><input type="hidden" data-field="session_id" '
           f'value="{_esc(session_id)}">'
           f'<button class="ghost" data-action="archive_chat" '
           f'data-reload="1">archiver</button><span data-status></span></span>'
           if session_id else "")
        + "</p>"
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


def athlete_section(athlete: dict | None, memory: list[dict] | None) -> str:
    """Who he is, and what the advisor has learned.

    This was the missing half of every answer the advisor gave. It read numbers
    and knew nothing else — not an age, not twenty-three years of training, not
    a diagnosis that explains a dip visible in his own data. Advice written
    without that is not neutral, and it reads cold because it is addressed to a
    stranger.
    """
    a = athlete or {}
    age = f' <span class="note">{a["age"]} ans</span>' if a.get("age") else ""
    mem = ""
    for m in (memory or [])[:12]:
        mem += (f'<div class="turn claude">{_esc(m["note"])}'
                f'<div class="note">{_esc(m["learned_at"][:10])}</div></div>')
    mem_block = (f'<details class="card"><summary>Ce que l\'assistant a retenu '
                 f'({len(memory or [])})</summary>{mem}</details>' if memory else "")

    return f"""<h2>Profil{age}</h2>
<div class="card" data-card>
  <label>naissance<input data-field="born_on" type="date"
    value="{_esc(a.get("born_on") or "")}"></label>
  <label>parcours
    <textarea data-field="background" rows="4"
      placeholder="depuis quand, quelles disciplines, blessures et antécédents qui comptent…"
      >{_esc(a.get("background"))}</textarea></label>
  <label>philosophie d'entraînement
    <textarea data-field="philosophy" rows="3"
      placeholder="comment tu abordes l'entraînement, ce qui compte pour toi…"
      >{_esc(a.get("philosophy"))}</textarea></label>
  <label>contraintes
    <textarea data-field="constraints" rows="3"
      placeholder="santé, disponibilité, ce qu'il faut éviter…"
      >{_esc(a.get("constraints"))}</textarea></label>
  <button data-action="set_profile" data-reload="1">Enregistrer</button>
  <span data-status></span>
</div>
{mem_block}"""


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


def sessions_section(sessions: list[dict], heading: str = "Séances",
                     more: str = "") -> str:
    """Sessions in the window, each with a place to say what the numbers cannot."""
    if not sessions:
        return (f"<h2>{heading}</h2><p class=\"note\">Aucune dans cette fenêtre — "
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
        if s.get("race"):
            flags += '<span class="flag race">course</span>'
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
    return f"<h2>{heading}</h2>" + "".join(cards) + (f"<p>{more}</p>" if more else "")


def questions_block(questions: list[dict] | None, *, heading: str = "Questions",
                    link_sessions: bool = False) -> str:
    """Open questions with a box to answer them, then the answered ones.

    These used to be sentences inside a review, which is a document with no
    reply box — and since a session is reviewed only once, the question sat
    somewhere that would never be rewritten. The athlete read them and had
    nowhere to put the answer; the next review asked again.

    Open ones come first and carry how often they have been asked, because a
    question on its third asking is telling you something the first was not.
    Answered ones stay visible rather than disappearing: what he said is the
    part worth keeping, and a question that vanishes on being answered gives no
    sign the loop closed.
    """
    if not questions:
        return ""
    open_ones = [q for q in questions if not q.get("answer")]
    answered = [q for q in questions if q.get("answer")]

    rows = []
    for q in open_ones:
        asked = _esc(q["asked_at"][:10])
        again = (f" · reposée {q['times_asked']}×" if q.get("times_asked", 1) > 1
                 else "")
        where = ""
        if link_sessions and q.get("workout_id"):
            where = (f' · <a href="/session/{int(q["workout_id"])}">'
                     f'la séance</a>')
        rows.append(
            f'<div class="card" data-card>'
            f'<p><strong>{_esc(q["question"])}</strong></p>'
            f'<p class="note">posée le {asked}{again}{where}</p>'
            f'<input type="hidden" data-field="key" value="{_esc(q["key"])}">'
            f'<textarea data-field="answer" rows="2" '
            f'placeholder="ta réponse…"></textarea>'
            f'<button data-action="answer_question" data-reload="1">Répondre</button>'
            f'<span data-status></span></div>')
    for q in answered:
        rows.append(
            f'<div class="card">'
            f'<p class="note">{_esc(q["question"])}</p>'
            f'<p>{_esc(q["answer"])}</p>'
            f'<p class="note">répondu le {_esc((q.get("answered_at") or "")[:10])}</p>'
            f'</div>')

    count = f" ({len(open_ones)})" if open_ones else ""
    return f'<h2>{_esc(heading)}{count}</h2>' + "".join(rows)


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


# How many month grids are still a calendar. Past this the thing stops being a
# view and becomes a wall: the race filter spans the whole record, which is 165
# months, and the page rendered every one of them.
CALENDAR_MONTHS = 3


def calendar_section(sessions: list[dict], start: date, end: date) -> str:
    """Which days had training, as a month grid.

    A list answers "what did I do on the 2nd"; this answers "how does the block
    look", which is the question a training week is actually judged on. Empty
    days are the information — three blanks in a row is the thing worth seeing.

    Only for a window that is calendar-sized. Over thirteen years there is no
    block to see the shape of, and scrolling 165 grids to find three races is
    worse than the list, which is already showing exactly those three. So it
    steps aside and says so rather than rendering something unusable.
    """
    months_spanned = ((end.year - start.year) * 12 + end.month - start.month) + 1
    if months_spanned > CALENDAR_MONTHS:
        return ('<p class="note">Fenêtre trop large pour un calendrier '
                f"({months_spanned} mois) — la liste ci-dessous fait le "
                "travail. Réduis la fenêtre pour retrouver la grille.</p>")

    by_day: dict[str, list[dict]] = {}
    for s in sessions:
        by_day.setdefault(s["date"], []).append(s)

    months: list[str] = []
    cursor = date(start.year, start.month, 1)
    while cursor <= end:
        if cursor.month == 12:
            nxt = date(cursor.year + 1, 1, 1)
        else:
            nxt = date(cursor.year, cursor.month + 1, 1)
        cells = ""
        # Monday-first, matching how the plan is written.
        for _ in range(cursor.weekday()):
            cells += '<span class="cal pad"></span>'
        day = cursor
        while day < nxt:
            key = day.isoformat()
            todays = by_day.get(key, [])
            if todays:
                initials = "".join(
                    _activity(s["activity"])[0].upper() for s in todays[:3])
                cells += (f'<a class="cal has" href="/seances?from={key}&to={key}" '
                          f'title="{_esc(", ".join(_activity(s["activity"]) for s in todays))}">'
                          f'<b>{day.day}</b><span>{_esc(initials)}</span></a>')
            else:
                cells += f'<span class="cal"><b>{day.day}</b></span>'
            day = day + timedelta(days=1)
        months.append(
            f'<div class="month"><h3>{cursor.strftime("%B %Y")}</h3>'
            f'<div class="grid7">{cells}</div></div>')
        cursor = nxt
    return f'<div class="card cals">{"".join(months)}</div>'


def filter_bar(sessions: list[dict], start: date, end: date,
               activity: str | None, races_only: bool) -> str:
    """Sport and race filters, plus what the window actually is.

    One row above the list, which is where a filter belongs. The counts are of
    the *unfiltered* window, so choosing one does not hide how much it removed
    — a filter that renumbers itself gives no way to tell an empty result from
    a wrong query.

    Links, not scripts: each is a URL, so a filtered view can be sent to
    yourself and comes back the same.
    """
    counts: dict[str, int] = {}
    for s in sessions:
        counts[s["activity"]] = counts.get(s["activity"], 0) + 1
    races = sum(1 for s in sessions if s.get("race"))

    def link(label: str, params: str, on: bool) -> str:
        cls = "chip on" if on else "chip"
        return f'<a class="{cls}" href="/seances?{params}">{_esc(label)}</a>'

    window = f"from={start.isoformat()}&to={end.isoformat()}"
    chips = [link(f"tout ({len(sessions)})", window, not activity and not races_only)]
    for act, n in sorted(counts.items(), key=lambda kv: -kv[1]):
        chips.append(link(f"{_activity(act)} ({n})", f"{window}&activity={act}",
                          activity == act))
    # Offered even when this window holds none: asking for races widens the
    # window to the whole record, so an empty count here is not an empty
    # answer. It is the one filter that is not a subset of what is on screen.
    chips.append(link(f"courses ({races})" if races else "courses",
                      "races=1", races_only))

    note = ""
    if races_only:
        note = ('<p class="note">« Courses » = les séances pour lesquelles '
                "<code>data/races/</code> contient une archive — trois à ce "
                "jour. Rien dans HealthKit ne dit qu'une séance était une "
                "course, donc c'est un plancher, pas un inventaire, et ce "
                "filtre couvre tout le dossier plutôt que la fenêtre.</p>")
    return f'<div class="filters">{"".join(chips)}</div>{note}'


def render_sessions(context: dict, sessions: list[dict], start: date,
                    end: date, activity: str | None = None,
                    races_only: bool = False) -> str:
    """Every session in the window, newest first, with a calendar above it.

    Newest first because the question asked of this page is almost always about
    the last few days; oldest-first meant scrolling to the bottom of a
    forty-five day window to reach yesterday. The calendar above keeps reading
    left-to-right through the month, which is how a month is read.
    """
    shown = sessions
    if activity:
        shown = [s for s in shown if s["activity"] == activity]
    if races_only:
        shown = [s for s in shown if s.get("race")]
    newest_first = sorted(shown, key=lambda s: s["date"], reverse=True)

    body = (
        "<h1>Séances</h1>"
        + coverage_line(context["coverage"])
        + window_summary(newest_first, start, end, context["record"])
        # Said in words, above the list. The window was implied by two arrows
        # and a pair of dates in the nav, so the page looked like the whole
        # record with an odd beginning.
        + f'<p class="cov">Fenêtre affichée : <b>{start.isoformat()}</b> → '
        f"<b>{end.isoformat()}</b> ({(end - start).days + 1} jours).</p>"
        + calendar_section(newest_first, start, end)
        + window_nav(start, end, context["record"])
        + filter_bar(sessions, start, end, activity, races_only)
        + sessions_section(newest_first))
    return _page(body, "/seances")


def render_jobs(jobs: list[dict]) -> str:
    """What the advisor is doing right now, and what it just finished.

    Analysing a session takes a minute or two behind the request, and until now
    the only sign of it was a small status line on the page that started it —
    leave that page and the work became invisible.
    """
    rows = ""
    for j in jobs:
        state = {"running": "en cours", "done": "terminé",
                 "failed": "échec"}.get(j["state"], j["state"])
        detail = (f'{j["elapsed"]}s' if j.get("elapsed") is not None
                  else _esc(str(j.get("error") or "")[:120]))
        rows += (f'<div class="chatrow"><span class="q">{_esc(j["action"])}</span>'
                 f'<span class="note">{state} · {detail}</span></div>')
    if not rows:
        rows = ('<p class="note">Rien en cours. Les analyses et le plan tournent '
                "ici quand tu les lances.</p>")
    body = ("<h1>En cours</h1>"
            '<p class="cov">Les travaux longs tournent derrière la requête ; '
            "cette page les montre où que tu sois.</p>"
            f'<div class="card">{rows}</div>')
    return _page(body, "/jobs")


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
        # High on the page, and only when something is outstanding. A question
        # the advisor cannot answer from the record is the one thing here that
        # needs *him* — everything else on this page it can work out alone — so
        # it goes above the session rather than below the plan. Answered ones
        # are not repeated here; they live on the session they came from.
        + questions_block([q for q in (context.get("open_questions") or [])],
                          heading="Questions ouvertes", link_sessions=True)
        # The last session, not forty-five days of them. The list is the whole
        # point of /seances; repeating it here pushed everything else off the
        # page and made the landing view a worse version of that one.
        + sessions_section(sessions[:1], heading="Dernière séance",
                           more='<a class="btn" href="/seances">toutes les séances →</a>')
        + plan_section(context.get("plan"))
        + goals_section(context["goals"])
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
        # Drawn twice. A single stroke — it was #e8590c — disappears into
        # OpenStreetMap's own orange trunk roads and its beige built-up fill;
        # on a city ride the track was findable only where it crossed a park.
        # The white casing underneath is the standard cartographic fix: it
        # separates the line from whatever it runs over, so the colour on top
        # no longer has to win against the basemap on its own.
        f'<polyline points="{track}" fill="none" stroke="#ffffff" '
        f'stroke-width="7" stroke-linejoin="round" stroke-linecap="round" '
        f'opacity="0.85"/>'
        f'<polyline points="{track}" fill="none" stroke="#7b2ff7" '
        f'stroke-width="3.5" stroke-linejoin="round" stroke-linecap="round"/>'
        # Where the scrubber is. Hidden until the profile is touched.
        f'<circle class="scrub-dot" r="7" fill="#7b2ff7" stroke="#ffffff" '
        f'stroke-width="3" opacity="0"/>'
        f"</svg></div></div>"
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
        # Cumulative distance along the sampled track, so the x axis is in
        # kilometres rather than "point number". Equirectangular is plenty at
        # this scale and the axis only carries three or four labels.
        cum, total = [0.0], 0.0
        for (la1, lo1), (la2, lo2) in zip(pts, pts[1:]):
            dy = (la2 - la1) * 111_320.0
            dx = (lo2 - lo1) * 111_320.0 * math.cos(math.radians((la1 + la2) / 2))
            total += math.hypot(dx, dy)
            cum.append(total)
        km = total / 1000.0

        idx = [i for i, e in enumerate(eles) if e is not None]
        if len(idx) > 1:
            # Plotted against distance, not index: a track sampled evenly in
            # time is not sampled evenly in space, and drawing it as though it
            # were bends every climb toward wherever he was slowest.
            pairs = [((cum[i] / total if total else i / max(len(eles) - 1, 1)) * 100.0,
                      30.0 - ((eles[i] - lo) / span * 28.0)) for i in idx]
            line = " ".join(f"{x:.2f},{y:.2f}" for x, y in pairs)
            area = f"{pairs[0][0]:.2f},30 {line} {pairs[-1][0]:.2f},30"
            mid = (lo + hi) / 2
            # Four ticks including zero; the last is the total, which is the
            # one number worth reading off this axis.
            xlabels = "".join(
                f"<span>{(km * f):.1f}</span>" for f in (0, 1 / 3, 2 / 3, 1))
            scrub = json.dumps({
                # One index space for both: `points` and `elevation` come out
                # of the same bucketed query, so profile position i *is* map
                # position i. Anything else here would drift them apart.
                "map": [[round(x, 1), round(y, 1)] for x, y in proj],
                "idx": idx,
                "x": [round(x, 2) for x, _ in pairs],
                "ele": [round(eles[i], 1) for i in idx],
                "km": [round(cum[i] / 1000.0, 2) for i in idx],
            }, separators=(",", ":"))
            profile = (
                f'<div class="profile-wrap" data-route '
                f"data-scrub='{_esc(scrub)}'>"
                f'<div class="yaxis"><span>{hi:.0f}</span><span>{mid:.0f}</span>'
                f'<span>{lo:.0f}</span></div>'
                f'<div class="plot">'
                f'<svg viewBox="0 0 100 30" class="profile" '
                f'preserveAspectRatio="none" role="img" aria-label="profil">'
                # Hairline, recessive, and behind the data.
                f'<line x1="0" y1="2" x2="100" y2="2" class="grid"/>'
                f'<line x1="0" y1="16" x2="100" y2="16" class="grid"/>'
                f'<line x1="0" y1="30" x2="100" y2="30" class="grid"/>'
                f'<polygon points="{area}" fill="currentColor" opacity="0.15"/>'
                f'<polyline points="{line}" fill="none" stroke="currentColor" '
                f'stroke-width="0.6" vector-effect="non-scaling-stroke"/>'
                f'<line class="scrub-line" x1="0" y1="0" x2="0" y2="30" '
                f'opacity="0" vector-effect="non-scaling-stroke"/>'
                f"</svg>"
                f'<div class="scrub-hit" role="slider" tabindex="0" '
                f'aria-label="parcourir le profil" aria-valuemin="0" '
                f'aria-valuemax="{km:.1f}" aria-valuenow="0"></div>'
                f'</div>'
                f'<div class="xaxis">{xlabels}</div>'
                f'<div class="unit">m</div><div class="unit km">km</div>'
                f"</div>"
                f'<p class="note"><span data-readout>{lo:.0f}–{hi:.0f} m sur '
                f"{km:.1f} km</span> — {len(idx)} points échantillonnés. "
                f"Glisse sur le profil pour situer un point sur la carte. "
                # The axis will not match the session's distance and should not
                # be read as disagreeing with it: measuring along a track cut
                # to 400 points shortens every bend, a few percent over a ride.
                f"Le kilométrage est mesuré le long du tracé échantillonné, "
                f"donc légèrement court par rapport à la distance de la "
                f"séance.</p>")

    return (f"<h2>Parcours</h2><div class=\"card\">{mapped}{profile}"
            f"</div>")


def zone_donut(hr: dict) -> str:
    """Time in each heart-rate zone, as a ring.

    Part-to-whole of five ordered slices, which is the one job a donut does
    better than a bar: the question here is "how was this session divided",
    not "which zone was biggest". The table below carries the exact minutes,
    so the ring never has to be read precisely.

    Colour is one hue getting darker, not five hues. Zones are **ordered** —
    Z1 to Z5 is a scale, not five unrelated things — and the training-app
    convention of blue/green/yellow/orange/red is a rainbow ramp: it implies
    Z3 differs from Z2 the way green differs from yellow, which is a category
    difference, and it puts the two most alarming colours on the two zones
    least often reached. A single ramp says "more" without saying "other".

    Slices are separated by a surface-coloured gap so adjacent zones stay
    distinct without an outline, and each slice is labelled directly when it
    is big enough to hold text — colour alone never carries the identity.
    """
    order = [l for l in hr.get("zone_percent", {}) if hr.get("zone_seconds", {}).get(l)]
    total = sum(hr["zone_seconds"][l] for l in order)
    if not order or total <= 0:
        return ""

    # Circumference arithmetic on r=1 keeps the numbers readable: a slice of
    # share s is s * 2pi of the ring.
    R, STROKE = 42.0, 22.0
    circ = 2 * math.pi * R
    GAP = 2.0                      # px of surface between slices, per marks spec
    out, offset, legend = [], 0.0, []
    for i, label in enumerate(order):
        share = hr["zone_seconds"][label] / total
        length = max(share * circ - GAP, 0.5)
        secs = int(hr["zone_seconds"][label])
        out.append(
            f'<circle class="slice z{i + 1}" r="{R}" cx="60" cy="60" '
            f'fill="none" stroke-width="{STROKE}" '
            f'stroke-dasharray="{length:.2f} {circ - length:.2f}" '
            f'stroke-dashoffset="{-offset:.2f}" '
            f'transform="rotate(-90 60 60)">'
            f'<title>{_esc(label)} — {secs // 60}:{secs % 60:02d} '
            f'({share * 100:.0f} %)</title></circle>')
        offset += share * circ
        legend.append(
            f'<span class="key"><i class="sw z{i + 1}"></i>{_esc(label)} '
            f'<b>{share * 100:.0f} %</b></span>')

    # The centre carries the total, which is what the ring is a division of —
    # a donut with an empty hole wastes the one place a reader looks first.
    mins = int(total // 60)
    return (f'<div class="donut-wrap">'
            f'<svg viewBox="0 0 120 120" class="donut" role="img" '
            f'aria-label="répartition du temps par zone de fréquence cardiaque">'
            f"{''.join(out)}"
            f'<text x="60" y="58" class="dnum">{mins}</text>'
            f'<text x="60" y="72" class="dlab">min</text>'
            f"</svg>"
            f'<div class="legend">{"".join(legend)}</div></div>')


# HealthKit quantity identifiers, minus the prefix the parser already strips,
# and what each is actually in. The units come from `SyncEngine.statsDict`,
# which converts to a canonical unit per type before writing the delta — so
# these are not a guess, they are that function's other half.
MEASURES = {
    "RunningPower": ("Puissance", "W"),
    "RunningSpeed": ("Vitesse", "km/h"),
    "RunningStrideLength": ("Longueur de foulée", "m"),
    "RunningVerticalOscillation": ("Oscillation verticale", "m"),
    "RunningGroundContactTime": ("Temps de contact au sol", "ms"),
    "CyclingPower": ("Puissance", "W"),
    "CyclingSpeed": ("Vitesse", "km/h"),
    "CyclingCadence": ("Cadence", "tr/min"),
    "WalkingSpeed": ("Vitesse", "km/h"),
    "RespiratoryRate": ("Fréquence respiratoire", "/min"),
    "ActiveEnergyBurned": ("Énergie active", "kcal"),
    "BasalEnergyBurned": ("Énergie de repos", "kcal"),
    "DistanceSwimming": ("Distance", "m"),
    "DistanceCycling": ("Distance", "m"),
    "DistanceWalkingRunning": ("Distance", "m"),
}

# `HKWorkoutEventType`, in words. "motionPaused" on screen is the enum leaking.
EVENT_KINDS = {
    "lap": "tour", "segment": "repère auto", "marker": "marqueur",
    "pause": "pause", "resume": "reprise",
    "motionPaused": "pause auto", "motionResumed": "reprise auto",
    "pauseOrResumeRequest": "demande pause/reprise",
}


def _measure(key: str) -> str:
    """The measure's name, falling back to the raw identifier."""
    return MEASURES.get(key, (key, ""))[0]


def _unit(key: str) -> str:
    """Its unit, or nothing rather than a wrong one."""
    return MEASURES.get(key, ("", ""))[1]


def _event_kind(kind: str) -> str:
    return EVENT_KINDS.get(kind, kind)


def segments_section(segments: list[dict] | None, events: list[dict] | None) -> str:
    """The legs of a workout, and the markers the watch left in it.

    Segments are what a structured session actually was — intervals, or the legs
    of a triathlon. The export names no activity per segment, so a leg shows the
    workout's own; that is stated rather than dressed up, because inventing
    "Swimming" for leg one of a triathlon would be a fact nobody recorded.
    """
    out = ""
    # A single "segment" is the workout itself — HealthKit gives every workout
    # one `HKWorkoutActivity`, and only a multi-sport one gets more. Printing a
    # one-row table headed "Segments" said nothing, and said it directly above
    # a "Marqueurs" line reading "61× segment", so the page appeared to claim
    # one segment and sixty-one at once. They are different things with the
    # same name: the leg, and the marker the watch drops inside it.
    if segments and len(segments) > 1:
        rows = ""
        for s in segments:
            dur = (f"{int(s['duration_s'] // 60)}:{int(s['duration_s'] % 60):02d}"
                   if s.get("duration_s") else "—")
            stats = s.get("stats") or {}
            hr = stats.get("HeartRate") or {}
            extra = f"{hr['avg']:.0f} bpm" if hr.get("avg") else ""
            rows += (f"<tr><td>{s['idx']}</td>"
                     f"<td>{_esc(_activity(s.get('activity')))}</td>"
                     f"<td>{_esc(s['started_at'][11:19])}</td>"
                     f"<td>{_esc(dur)}</td><td>{_esc(extra)}</td></tr>")
        out += ("<h2>Legs</h2><div class=\"card\"><table>"
                "<tr><th>#</th><th>sport</th><th>début</th><th>durée</th>"
                f"<th>FC moy</th></tr>{rows}</table>"
                '<p class="note">Les segments multisports enregistrés par la '
                "montre — les legs d'un triathlon. L'export ne nomme pas le "
                "sport de chaque leg ; celui de la séance est repris.</p></div>")
    elif segments:
        # One leg, but its `stats` carry measures held nowhere else — running
        # power, stride length, ground-contact time, vertical oscillation —
        # which the one-row table reduced to "9 mesure(s)" and threw away.
        stats = segments[0].get("stats") or {}
        shown = "".join(
            f"<tr><td>{_esc(_measure(k))}</td>"
            f"<td>{v.get('avg', v.get('sum', 0)):.1f}</td>"
            f"<td>{_esc(_unit(k))}</td></tr>"
            for k, v in sorted(stats.items())
            if k != "HeartRate" and isinstance(v, dict)
            and (v.get("avg") is not None or v.get("sum") is not None))
        if shown:
            out += ('<h2>Mesures de la montre</h2><div class="card"><table>'
                    f"<tr><th>mesure</th><th>valeur</th><th>unité</th></tr>"
                    f"{shown}</table>"
                    '<p class="note">Relevées sur la séance entière. Moyenne, '
                    "sauf pour les cumuls (distance, énergie).</p></div>")
    if events:
        chips = " · ".join(f"{e['count']}× {_esc(_event_kind(e['kind']))}"
                           for e in events)
        out += (f'<h2>Marqueurs</h2><div class="card"><p>{chips}</p>'
                '<p class="note">Repères posés par la montre pendant la '
                "séance — tours automatiques, pauses, reprises. Sans rapport "
                "avec les legs multisports ci-dessus, qui portent hélas le "
                "même nom chez Apple.</p></div>")
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

    questions_section = questions_block(detail.get("questions"))

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
        # Named, not passed through. This printed a bare "1" for every archived
        # swim — the export writes the enum's integer — and stripping a prefix
        # the export never wrote was the only handling it had. "bassin" is
        # dropped when the lap length two entries up has already said it, which
        # otherwise reads "bassin 25 m · bassin"; open water has nothing else
        # announcing it, so it always earns its place.
        {"pool": "" if s.get("pool_length_m") is not None else "bassin",
         "openWater": "eau libre"}.get(s.get("swim_location"), ""),
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
            + zone_donut(hr)
            + f"<table><tr><th>zone</th><th>time</th><th>share</th></tr>{rows}</table>"
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
        + coverage_line(detail["coverage"]) + review_section + questions_section
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
