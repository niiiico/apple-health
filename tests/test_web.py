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
