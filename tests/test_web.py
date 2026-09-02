"""Tests for the interaction layer's rules and rendering.

The actions are plain functions of `(store, payload)` precisely so they can be
tested without HTTP, and the rendering is a pure function of already-fetched
rows for the same reason. This file is the payoff for that split.
"""

from __future__ import annotations

import html
import threading
from datetime import date

import pytest

from apple_health import queries, ui, web
from apple_health.derive.zones import ZONES, zone_of


class _Cur:
    """A cursor stub recording what it was asked, answering what it was told."""

    def __init__(self, answers=None):
        self.answers = list(answers or [])
        self.executed = []
        # Real cursors expose rowcount; actions that check it to detect "no such
        # row" need a default, and 1 (a row was touched) is the benign one.
        self.rowcount = 1

    def execute(self, sql, params=None):
        self.executed.append((" ".join(sql.split()), params))

    def fetchone(self):
        return self.answers.pop(0) if self.answers else None

    def fetchall(self):
        return self.answers.pop(0) if self.answers else []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _Store:
    def __init__(self, answers=None):
        self.cur = _Cur(answers)
        self.committed = False
        self.rolled_back = False
        self.closed = False

    def cursor(self):
        return self.cur

    def commit(self):
        self.committed = True

    def rollback(self):
        self.rolled_back = True

    def close(self):
        self.closed = True


# --- zone bands --------------------------------------------------------------
# There is one zone model, the constant in derive.zones, and no way to record
# another. The dated-model machinery was removed because it reported bands it
# did not classify with; these tests exist so that cannot come back unnoticed.

def test_the_reported_bands_are_the_ones_used_to_classify():
    """The invariant the removed machinery broke.

    `_zone_basis` states the bands a caller should read the numbers against. If
    a band says 160-169 while `zone_of(165)` answers something other than that
    band's label, every zone figure is being reported against a model that did
    not produce it — which is exactly what happened when a recorded model was
    displayed but never reached the arithmetic.
    """
    for label, span in queries._zone_basis()["boundaries"].items():
        lo, hi = span.split("-")
        probe = float(lo) + 1 if hi == "inf" else (float(lo) + float(hi)) / 2
        assert zone_of(probe) == label, f"{probe} bpm is reported as {label}"


def test_there_is_no_way_to_record_a_zone_model():
    """Recording one would re-open the divergence, since nothing classifies by it."""
    assert "set_zone_model" not in web.ACTIONS


# --- goals -------------------------------------------------------------------

def test_a_goal_needs_words():
    with pytest.raises(ValueError, match="required"):
        web.set_goal(_Store(), {"goal": "   ", "target_date": "2026-10-04"})


def test_a_goal_needs_no_date():
    """Plenty of goals have no date, and requiring one would invite a fake."""
    store = _Store([{"id": 3}])
    msg = web.set_goal(store, {"goal": "stay in remission, keep load even"})
    assert "#3" in msg["message"] and store.committed
    sql, params = store.cur.executed[0]
    assert "INSERT INTO goals" in sql and params[1] is None


def test_a_goal_records_its_date_when_given():
    store = _Store([{"id": 4}])
    msg = web.set_goal(store, {"goal": "sub-1:50 half", "target_date": "2026-11-15"})
    assert "2026-11-15" in msg["message"]
    assert store.cur.executed[0][1][1] == date(2026, 11, 15)


def test_a_goal_with_an_unparseable_date_is_refused():
    with pytest.raises(ValueError, match="ISO date"):
        web.set_goal(_Store(), {"goal": "sub-1:50 half", "target_date": "novemberish"})


def test_archiving_an_unknown_goal_is_refused():
    """Silently succeeding would report a goal retired that is still driving plans."""
    store = _Store()
    store.cur.rowcount = 0
    with pytest.raises(ValueError, match="no active goal"):
        web.archive_goal(store, {"id": 99})


def test_archiving_keeps_the_row():
    """A plan written towards a goal is unintelligible if the goal is deleted."""
    store = _Store()
    store.cur.rowcount = 1
    web.archive_goal(store, {"id": 7})
    sql, _ = store.cur.executed[0]
    assert "UPDATE goals SET archived_at" in sql and "DELETE" not in sql


def test_no_goals_is_called_out_rather_than_left_blank():
    assert "rien vers" in ui.goals_section([])


# --- period notes ------------------------------------------------------------

def test_a_period_cannot_end_before_it_starts():
    with pytest.raises(ValueError, match="before it starts"):
        web.set_period_note(_Store(), {"starts_on": "2026-08-12",
                                       "ends_on": "2026-07-16", "note": "backwards"})


def test_an_empty_period_note_is_refused():
    # A blank period records nothing but looks like context was captured.
    with pytest.raises(ValueError, match="records nothing"):
        web.set_period_note(_Store(), {"starts_on": "2026-08-12", "note": "   "})


def test_an_open_ended_period_is_allowed():
    store = _Store()
    web.set_period_note(store, {"starts_on": "2026-07-16", "note": "pool shut"})
    assert store.cur.executed[0][1][1] is None, "ends_on should stay open"


# --- session notes -----------------------------------------------------------

def test_a_note_on_an_unknown_workout_is_refused():
    with pytest.raises(ValueError, match="no workout"):
        web.set_session_note(_Store(answers=[None]), {"workout_id": 999999, "note": "x"})


def test_an_emptied_note_deletes_rather_than_storing_blank():
    store = _Store(answers=[{"?column?": 1}, {"body": "douleur hanche droite"}])
    msg = web.set_session_note(store, {"workout_id": 1, "note": "  "})
    assert "effacée" in msg["message"]
    sql = " | ".join(s for s, _ in store.cur.executed)
    assert "DELETE FROM session_notes" in sql
    # Clearing the box was the easiest way to lose a note, and it left nothing
    # behind at all. The superseded text is kept.
    assert "INSERT INTO revisions" in sql


# --- rendering ---------------------------------------------------------------

def test_the_page_always_states_its_coverage():
    assert "unknown, not absent" in ui.coverage_line(
        {"observed_through": "2026-08-26T09:54:01+00:00"})


def test_an_empty_record_says_so_rather_than_rendering_a_blank():
    assert "empty" in ui.coverage_line({"observed_through": None})


def test_the_page_states_the_bands_it_computed_with():
    """"Z3 4:52" means nothing without them."""
    page = ui.zone_bands_section(queries._zone_basis())
    for label, _lo, _hi in ZONES:
        # Escaped, because "Z1 <135" carries a < that must not reach the DOM raw.
        assert html.escape(label) in page


def test_notes_are_escaped_into_the_page():
    html = ui.sessions_section([{"id": 1, "date": "2026-08-25", "activity": "Swimming",
                                 "distance_km": 1.65, "duration_min": 47, "avg_hr": 139,
                                 "max_hr": 171, "has_hr_series": True, "has_laps": False,
                                 "note": '<script>alert("x")</script>'}])
    assert "<script>alert" not in html and "&lt;script&gt;" in html


# --- window navigation -------------------------------------------------------

def test_the_window_defaults_to_the_recent_span():
    from datetime import date, timedelta

    start, end = web.window_for({}, 45)
    assert end == date.today() and (end - start).days == 45


def test_any_range_in_the_record_is_reachable():
    # The page used to be pinned to a recent window, so the France block was
    # unreachable from September. It is a view now, not the extent of the data.
    from datetime import date

    assert web.window_for({"from": ["2019-06-01"], "to": ["2019-06-30"]}, 45) == (
        date(2019, 6, 1), date(2019, 6, 30))


def test_a_reversed_range_is_swapped_rather_than_empty():
    from datetime import date

    assert web.window_for({"from": ["2026-08-26"], "to": ["2026-08-01"]}, 45) == (
        date(2026, 8, 1), date(2026, 8, 26))


def test_a_mistyped_date_falls_back_instead_of_erroring():
    from datetime import date

    start, end = web.window_for({"from": ["not-a-date"], "to": ["2026-08-26"]}, 10)
    assert end == date(2026, 8, 26) and (end - start).days == 10


# --- session detail ----------------------------------------------------------

_DETAIL = {
    "coverage": {"observed_through": "2026-08-26T09:54:01+00:00"},
    "zone_model": {"source": "default"},
    "session": {"id": 1, "date": "2026-08-25", "started_at": "2026-08-25T12:46:00+09:00",
                "tz": "UTC+09:00", "activity": "Swimming", "distance_km": 1.65,
                "duration_min": 47, "avg_hr": 139, "max_hr": 171, "energy_kcal": 369,
                "note": None},
    "hr": {"samples": 562, "avg": 140.0, "min": 78, "max": 171,
           "zone_percent": {"Z1 <135": 35.8, "Z2 135-159": 52.7},
           "zone_seconds": {"Z1 <135": 1041, "Z2 135-159": 1486},
           "drift_thirds": [{"third": "1/3", "avg": 126.0, "max": 154}]},
    "laps": None,
}


def test_the_detail_page_shows_durations_not_only_shares():
    # "62 % in Z2" and "24 minutes in Z2" answer different questions, and a
    # training plan asks the second one.
    html = ui.render_session(_DETAIL)
    assert "17:21" in html and "24:46" in html


def test_a_default_zone_model_is_named_on_the_page():
    assert "default" in ui.render_session(_DETAIL)


def test_a_session_with_no_series_says_so_rather_than_showing_zeroes():
    detail = {**_DETAIL, "hr": None}
    html = ui.render_session(detail)
    assert "No series recorded" in html and "different from a flat one" in html


def test_an_unknown_session_renders_an_error_page():
    assert "no workout with id 9" in ui.render_session({"error": "no workout with id 9"})


# --- probes ------------------------------------------------------------------
# Worth the cost of a real socket: the whole point of /livez is *which*
# dependencies it does not have, and that cannot be asserted by calling a pure
# function. The DSN below is deliberately unparseable, so any code path that
# reaches for the database fails immediately rather than hanging.

import http.server
import json as _json
import threading
import urllib.request
from contextlib import contextmanager


@contextmanager
def _serving(dsn):
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), web.handler_for(dsn))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_livez_answers_without_a_database():
    """Liveness must not depend on Postgres.

    If it did, someone else's database outage would restart this pod in a loop
    instead of taking it out of service — which cannot fix the database and,
    with one replica, keeps the site down longer.
    """
    with _serving("this is not a dsn") as base:
        with urllib.request.urlopen(f"{base}/livez", timeout=5) as r:
            assert r.status == 200
            assert _json.loads(r.read())["ok"] is True


def test_healthz_fails_when_the_database_is_unreachable():
    """Readiness must depend on Postgres — that is the difference from /livez."""
    with _serving("this is not a dsn") as base:
        try:
            urllib.request.urlopen(f"{base}/healthz", timeout=5)
        except urllib.error.HTTPError as exc:
            assert exc.code == 503
            assert _json.loads(exc.read())["ok"] is False
        else:
            pytest.fail("/healthz reported healthy with no reachable database")


# --- asking Claude -----------------------------------------------------------

def test_analysing_an_unknown_session_is_refused():
    """Otherwise a typo spends a minute of model time and stores a review of nothing."""
    with pytest.raises(ValueError, match="no session"):
        web.review_session(_Store([None]), {"workout_id": 999999})


def test_analysing_needs_a_numeric_id():
    with pytest.raises(ValueError, match="whole number"):
        web.review_session(_Store(), {"workout_id": "../etc/passwd"})


def test_a_chat_turn_needs_a_message():
    with pytest.raises(ValueError, match="required"):
        web.chat(_Store(), {"message": "   "})


def test_an_enormous_chat_message_is_refused():
    with pytest.raises(ValueError, match="too long"):
        web.chat(_Store(), {"message": "x" * 4001})


def test_a_chat_turn_threads_the_conversation(monkeypatch):
    """Without the session id every turn starts over and forgets the question before."""
    seen = {}

    def _chat(message, session_id=None, **kw):
        seen["message"], seen["session"] = message, session_id
        seen["history"] = kw.get("history")
        from apple_health.advisor import Run
        return Run("ça tient.", [{"query": "session", "args": {"id": 1}}],
                   session_id="sess-2")

    monkeypatch.setattr("apple_health.advisor.chat", _chat)
    store = _Store([[{"question": "et hier ?", "answer": "bien"}],  # history
                    {"hi": 0},                                     # writes before
                    []])                                           # writes after
    out = web.chat(store, {"message": "et mardi ?", "session_id": "sess-1"})
    assert seen["message"] == "et mardi ?" and seen["session"] == "sess-1"
    # The stored transcript travels with the turn, so a resume that fails
    # because the pod restarted can rebuild the conversation instead of
    # telling someone their chat is gone while it is on the screen.
    assert seen["history"] == [{"question": "et hier ?", "answer": "bien"}]
    # The conversation's own id, not the CLI's — see
    # test_a_rebuilt_thread_keeps_its_original_id.
    assert out["reply"] == "ça tient." and out["session_id"] == "sess-1"
    assert out["queries"] == ["session"]


def test_a_chat_turn_is_stored_with_its_question(monkeypatch):
    """History is kept: an answer worth acting on is worth re-reading.

    Question and answer go in one row because they are only ever read together,
    and a half-stored exchange is a question with no answer.
    """
    from apple_health.advisor import Run
    monkeypatch.setattr("apple_health.advisor.chat",
                        lambda *a, **k: Run("oui", [{"query": "context"}],
                                            session_id="s-1"))
    store = _Store([{"hi": 0}, []])
    web.chat(store, {"message": "alors ?"})
    sql, params = store.cur.executed[-1]
    assert "INSERT INTO chat_turns" in sql
    assert params[0] == "s-1" and params[1] == "alors ?" and params[2] == "oui"
    assert store.committed


def test_a_failed_turn_stores_no_question(monkeypatch):
    """A question with no answer is a worse record than no record."""
    def _boom(*a, **k):
        raise RuntimeError("claude exited 1")

    monkeypatch.setattr("apple_health.advisor.chat", _boom)
    store = _Store([{"hi": 0}])
    with pytest.raises(RuntimeError):
        web.chat(store, {"message": "alors ?"})
    assert not any("INSERT INTO chat_turns" in s for s, _ in store.cur.executed)
    assert not store.committed


# --- slow actions run behind the request -------------------------------------
# A phone cannot hold an HTTP connection open for two minutes: a screen lock, an
# app switch or a moment of bad signal drops it, and the browser reports the loss
# as a failure while the work is already running and its result already stored.

def test_slow_actions_are_the_ones_that_ask_claude():
    assert web.SLOW == {"chat", "retry_turn", "review_session", "write_plan"}
    # Everything else stays a single request — a note must save instantly.
    assert "set_session_note" not in web.SLOW and "set_goal" not in web.SLOW


def test_a_job_reports_running_then_its_result(monkeypatch):
    import time as _t
    release = threading.Event()
    monkeypatch.setitem(web.ACTIONS, "chat",
                        lambda store, p: release.wait(5) and {"reply": "voilà"})
    monkeypatch.setattr(web, "Store", lambda dsn: _Store())

    job = web.start_job(None, "chat", {"message": "?"})
    assert web.job_state(job)["state"] == "running"
    release.set()
    for _ in range(50):
        if web.job_state(job)["state"] != "running":
            break
        _t.sleep(0.1)
    state = web.job_state(job)
    assert state["state"] == "done" and state["result"] == {"reply": "voilà"}


def test_a_failing_job_reports_why(monkeypatch):
    import time as _t

    def _boom(store, payload):
        raise RuntimeError("claude exited 1")

    monkeypatch.setitem(web.ACTIONS, "chat", _boom)
    monkeypatch.setattr(web, "Store", lambda dsn: _Store())
    job = web.start_job(None, "chat", {"message": "?"})
    for _ in range(50):
        if web.job_state(job)["state"] != "running":
            break
        _t.sleep(0.1)
    state = web.job_state(job)
    assert state["state"] == "failed" and "claude exited 1" in state["error"]


def test_an_unknown_job_does_not_imply_the_work_failed():
    """The result is stored where it belongs, so a lost handle is not lost work."""
    state = web.job_state("nope")
    assert state["state"] == "unknown"
    assert "rechargement" in state["error"]


def test_a_job_records_its_result_even_if_the_connection_will_not_close(monkeypatch):
    """Recording the outcome must not depend on a clean close.

    It used to: a close() that raised skipped the write and left the job
    reporting "running" for ever — the one state a client cannot recover from,
    because it never stops polling and never learns anything.
    """
    import time as _t

    class _Awkward(_Store):
        def close(self):
            raise OSError("connection already gone")

    monkeypatch.setitem(web.ACTIONS, "chat", lambda store, p: {"reply": "quand même"})
    monkeypatch.setattr(web, "Store", lambda dsn: _Awkward())
    job = web.start_job(None, "chat", {"message": "?"})
    for _ in range(50):
        if web.job_state(job)["state"] != "running":
            break
        _t.sleep(0.1)
    state = web.job_state(job)
    assert state["state"] == "done" and state["result"] == {"reply": "quand même"}


# --- editing a question ------------------------------------------------------

def test_editing_a_turn_drops_the_answers_that_followed(monkeypatch):
    """An answer three turns later was formed from the question being edited.

    Leaving it would produce a thread whose later replies respond to words
    nobody ever said.
    """
    from apple_health.advisor import Run
    monkeypatch.setattr("apple_health.advisor.chat",
                        lambda *a, **k: Run("nouvelle réponse", [], session_id="s2"))
    store = _Store([{"session_id": "s1", "asked_at": date(2026, 8, 29)},
                    [{"question": "avant", "answer": "ok"}]])
    store.cur.rowcount = 3
    out = web.retry_turn(store, {"turn_id": 7, "message": "question corrigée"})
    sql = " | ".join(s for s, _ in store.cur.executed)
    assert "DELETE FROM chat_turns" in sql
    assert "INSERT INTO chat_turns" in sql
    assert out["reply"] == "nouvelle réponse"


def test_an_edited_turn_does_not_resume_the_old_session(monkeypatch):
    """The old session still holds the original question.

    Resuming it would answer the new wording with the old one still in mind —
    the edit would appear to take and quietly not have.
    """
    seen = {}

    def _chat(message, session_id=None, **kw):
        seen["session_id"] = session_id
        seen["history"] = kw.get("history")
        from apple_health.advisor import Run
        return Run("r", [], session_id="fresh")

    monkeypatch.setattr("apple_health.advisor.chat", _chat)
    store = _Store([{"session_id": "s1", "asked_at": date(2026, 8, 29)},
                    [{"question": "avant", "answer": "ok"}]])
    web.retry_turn(store, {"turn_id": 7, "message": "corrigée"})
    assert seen["session_id"] is None
    assert seen["history"] == [{"question": "avant", "answer": "ok"}]


def test_editing_an_unknown_turn_is_refused():
    with pytest.raises(ValueError, match="no turn"):
        web.retry_turn(_Store([None]), {"turn_id": 999, "message": "x"})


def test_a_rebuilt_thread_keeps_its_original_id(monkeypatch):
    """A failed resume starts a fresh CLI session; the conversation is the same.

    Keying the stored turn on the CLI's new id would split one thread into two
    rows in the list — it would appear to fork on every deploy, which is exactly
    when resumes fail.
    """
    from apple_health.advisor import Run
    monkeypatch.setattr("apple_health.advisor.chat",
                        lambda *a, **k: Run("suite", [], session_id="cli-new"))
    store = _Store([[{"question": "avant", "answer": "ok"}], {"hi": 0}, []])
    out = web.chat(store, {"message": "et après ?", "session_id": "thread-1"})
    _sql, params = store.cur.executed[-1]
    assert params[0] == "thread-1"
    assert out["session_id"] == "thread-1"


# --- the pages exist ---------------------------------------------------------
# Every renderer a route calls, asserted to exist and to produce a page. These
# are cheap and would have caught a real outage: rewriting an adjacent function
# deleted `render_chats` and `render_chat` outright, and nothing failed until
# the link was clicked, because no test referenced them.

@pytest.mark.parametrize("name", [
    "render", "render_session", "render_chats", "render_chat", "render_error",
])
def test_every_renderer_a_route_calls_still_exists(name):
    assert callable(getattr(ui, name, None)), f"ui.{name} is gone"


def test_the_conversation_list_renders():
    page = ui.render_chats([
        {"session_id": "s1", "first_question": "et mardi ?",
         "last_at": "2026-08-30T10:00:00", "turns": 2}])
    assert "et mardi ?" in page and "/chat/s1" in page


def test_an_empty_conversation_list_still_renders():
    assert "Discussions" in ui.render_chats([])


def test_a_conversation_renders_its_turns():
    page = ui.render_chat("s1", [
        {"id": 1, "asked_at": "2026-08-30T10:00:00", "question": "et mardi ?",
         "answer": "correct.", "queries": ["session"]}])
    assert "et mardi ?" in page and "correct." in page
    # The rewrite affordance is what makes a wrong question fixable.
    assert 'data-edit="1"' in page


def test_the_error_page_hides_the_message_and_keeps_the_class():
    """A psycopg message carries the DSN — host, database and user."""
    page = ui.render_error(RuntimeError("dbname=apple_health host=postgres.int"))
    assert "RuntimeError" in page
    assert "dbname=apple_health" not in page


def test_the_archiver_skips_a_blank_previous_note():
    """An empty note is not a version worth keeping."""
    from apple_health.store import archive_note
    cur = _Cur([{"body": "   "}])
    assert archive_note(cur, 1, "athlete") is False
    assert not any("INSERT INTO revisions" in s for s, _ in cur.executed)


def test_the_archiver_keeps_a_real_previous_note():
    from apple_health.store import archive_note
    cur = _Cur([{"body": "douleur hanche droite"}])
    assert archive_note(cur, 1, "advisor") is True
    sql, params = cur.executed[-1]
    assert "INSERT INTO revisions" in sql
    assert params == ("session_notes", "1", "douleur hanche droite", "advisor")


def test_the_versions_page_renders_what_something_used_to_say():
    page = ui.render_versions("goals", "1", [
        {"body": "sub-3:05", "archived_at": "2026-09-01T10:00:00",
         "replaced_by": "athlete"}], label="sub-3:00")
    assert "sub-3:05" in page and "toi" in page


def test_an_empty_version_history_still_renders():
    assert "Aucune version" in ui.render_versions("goals", "1", [])
