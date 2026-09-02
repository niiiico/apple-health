"""Tests for the advisor's write surface.

The point of these is the boundary. The model can now write, and the guarantee
that makes that acceptable is that it writes only what a person writes by hand —
never what the watch measured.
"""

from __future__ import annotations

import pytest

from apple_health import advisor, write_cli


class _Cur:
    def __init__(self, answers=None):
        self.answers = list(answers or [])
        self.executed = []
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

    def cursor(self):
        return self.cur

    def commit(self):
        self.committed = True


SENSOR_TABLES = ("workouts", "hr_samples", "laps", "route_points",
                 "daily_metrics", "records", "routes", "workout_segments")


def test_no_subcommand_can_touch_what_the_watch_measured():
    """The boundary the write access rests on.

    A model editing measured data would corrupt the one thing this project
    exists to keep honest, and it would look exactly like data.
    """
    import inspect
    source = inspect.getsource(write_cli)
    for table in SENSOR_TABLES:
        assert f"INSERT INTO {table}" not in source, table
        assert f"UPDATE {table}" not in source, table
        assert f"DELETE FROM {table}" not in source, table


def test_the_advisor_may_run_only_these_two_commands():
    assert advisor.ALLOWED_TOOLS == "Bash(ah-query:*),Bash(ah-write:*)"


def test_a_note_records_what_it_replaced():
    """Losing something he wrote himself is the worst outcome available here."""
    store = _Store([{"id": 5560}, {"note": "ancienne note"}, {"body": "ancienne note"}])
    write_cli.write_note(store, 5560, "nouvelle note")
    audit = [p for s, p in store.cur.executed if "advisor_writes" in s]
    assert audit and audit[0][4] == '"ancienne note"'
    assert store.committed


def test_a_note_on_an_unknown_session_is_refused():
    with pytest.raises(SystemExit, match="no session"):
        write_cli.write_note(_Store([None]), 999999, "x")


def test_appending_to_a_document_keeps_what_was_there():
    """Amending a plan is the common case; rewriting six thousand words is not."""
    store = _Store([{"body": "Semaine 1"}])
    write_cli.write_doc(store, "plan", "Semaine 2", append=True)
    body = [p for s, p in store.cur.executed if "INSERT INTO documents" in s][0][1]
    assert body == "Semaine 1\n\nSemaine 2"


def test_replacing_a_document_says_so_and_keeps_the_original():
    store = _Store([{"body": "x" * 6000}])
    summary = write_cli.write_doc(store, "plan", "court", append=False)
    assert "réécrit" in summary and "6000" in summary
    audit = [p for s, p in store.cur.executed if "advisor_writes" in s][0]
    assert audit[4] == '"' + "x" * 6000 + '"'


def test_amending_a_goal_keeps_the_previous_wording():
    """A goal whose wording moves is the same goal.

    Archiving one to write another loses the thread of what was being aimed at
    and why it changed, which is most of what a goal's history is worth.
    """
    store = _Store([{"goal": "sub-3:05"}, {"body": "sub-3:05"}])
    summary = write_cli.write_goal(store, "sub-3:00", None, goal_id=1)
    sql = " | ".join(s for s, _ in store.cur.executed)
    assert "INSERT INTO revisions" in sql and "UPDATE goals" in sql
    assert "modifié" in summary


def test_amending_an_unknown_goal_is_refused():
    with pytest.raises(SystemExit, match="no active goal"):
        write_cli.write_goal(_Store([None]), "x", None, goal_id=999)


def test_a_document_keeps_what_it_replaced_as_a_version():
    store = _Store([{"body": "ancien plan"}, {"body": "ancien plan"}])
    write_cli.write_doc(store, "plan", "nouveau plan", append=False)
    assert any("INSERT INTO revisions" in s for s, _ in store.cur.executed)


# --- note history ------------------------------------------------------------

def test_the_advisor_archives_before_replacing():
    """Both write paths archive; neither may be the one that forgets."""
    store = _Store([{"id": 5560}, {"note": "ancienne"}, {"body": "ancienne"}])
    write_cli.write_note(store, 5560, "nouvelle")
    sql = " | ".join(s for s, _ in store.cur.executed)
    assert "INSERT INTO revisions" in sql
