"""Tests for the interaction layer's rules and rendering.

The actions are plain functions of `(store, payload)` precisely so they can be
tested without HTTP, and the rendering is a pure function of already-fetched
rows for the same reason. This file is the payoff for that split.
"""

from __future__ import annotations

import pytest

from apple_health import ui, web


class _Cur:
    """A cursor stub recording what it was asked, answering what it was told."""

    def __init__(self, answers=None):
        self.answers = list(answers or [])
        self.executed = []

    def execute(self, sql, params=None):
        self.executed.append((" ".join(sql.split()), params))

    def fetchone(self):
        return self.answers.pop(0) if self.answers else None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _Store:
    def __init__(self, answers=None):
        self.cur = _Cur(answers)
        self.committed = False

    def cursor(self):
        return self.cur

    def commit(self):
        self.committed = True


# --- zone models -------------------------------------------------------------

def test_zone_bands_must_ascend():
    # An out-of-order model misclassifies every session it covers, and nothing
    # downstream ever signals that it did.
    with pytest.raises(ValueError, match="ascend"):
        web.set_zone_model(_Store(), {"effective_from": "2026-01-15", "z1_max": 170,
                                      "z2_max": 159, "z3_max": 169, "z4_max": 177})


def test_zone_model_needs_a_parseable_date():
    with pytest.raises(ValueError, match="ISO date"):
        web.set_zone_model(_Store(), {"effective_from": "nope", "z1_max": 1,
                                      "z2_max": 2, "z3_max": 3, "z4_max": 4})


def test_zone_model_needs_all_four_bounds():
    with pytest.raises(ValueError, match="z1_max"):
        web.set_zone_model(_Store(), {"effective_from": "2026-01-15", "z1_max": 134})


def test_a_valid_zone_model_is_written_and_committed():
    store = _Store()
    msg = web.set_zone_model(store, {"effective_from": "2026-01-15", "source": "lab",
                                     "z1_max": 134, "z2_max": 159, "z3_max": 169,
                                     "z4_max": 177, "note": "ramp test"})
    assert "2026-01-15" in msg["message"] and store.committed
    sql, params = store.cur.executed[0]
    assert "hr_zone_models" in sql and params[1] == "lab"


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
    store = _Store(answers=[{"?column?": 1}])
    assert web.set_session_note(store, {"workout_id": 1, "note": "  "})["message"] == "note cleared"
    assert "DELETE FROM session_notes" in store.cur.executed[1][0]


# --- rendering ---------------------------------------------------------------

def test_the_page_always_states_its_coverage():
    assert "unknown, not absent" in ui.coverage_line(
        {"observed_through": "2026-08-26T09:54:01+00:00"})


def test_an_empty_record_says_so_rather_than_rendering_a_blank():
    assert "empty" in ui.coverage_line({"observed_through": None})


def test_an_empty_zone_timeline_is_called_out():
    # Silence here would let every zone figure in the system quietly use the
    # built-in bands while looking recorded.
    assert "built-in bands" in ui.zone_models_section([{"source": "default"}])


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
