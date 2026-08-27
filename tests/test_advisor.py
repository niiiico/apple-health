"""Tests for the advisor's loop and its bookkeeping.

The loop is exercised against a fake client rather than the API: what matters
here is that tool calls are dispatched and *recorded*, that a run which never
concludes fails instead of storing half an opinion, and that a broken tool is
reported to the model rather than ending the run. None of that needs a network.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from types import SimpleNamespace

import pytest

from apple_health import advisor


class _Block(SimpleNamespace):
    pass


def _text(msg: str) -> _Block:
    return _Block(type="text", text=msg)


def _tool(name: str, tid: str = "t1", **inp) -> _Block:
    return _Block(type="tool_use", name=name, id=tid, input=inp)


class _Response(SimpleNamespace):
    pass


class _Client:
    """Replays a scripted list of responses, recording the requests it got."""

    def __init__(self, responses):
        self._responses = list(responses)
        self.requests = []
        self.messages = self

    def create(self, **kwargs):
        self.requests.append(kwargs)
        if not self._responses:
            raise AssertionError("the loop asked for more turns than were scripted")
        return self._responses.pop(0)


def _resp(content, stop_reason):
    return _Response(content=content, stop_reason=stop_reason,
                     usage=SimpleNamespace(input_tokens=10, output_tokens=5))


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

    def coverage(self, *a, **k):
        return SimpleNamespace(
            observed_through=datetime(2026, 8, 26, 9, 54, tzinfo=timezone.utc))


# --- the loop ----------------------------------------------------------------

def test_a_run_with_no_tool_calls_returns_its_text():
    client = _Client([_resp([_text("  Easy walk, nothing to flag.  ")], "end_turn")])
    run = advisor.converse(client, _Store(), "review it")
    assert run.text == "Easy walk, nothing to flag."
    assert run.calls == []


def test_every_tool_call_is_recorded(monkeypatch):
    """`basis` is only worth storing if it is complete.

    A review is an opinion about data; the record of which queries produced it
    is what makes it checkable a year later.
    """
    monkeypatch.setattr(advisor, "_dispatch", lambda s, n, a: {"ok": n})
    client = _Client([
        _resp([_tool("context", "a")], "tool_use"),
        _resp([_tool("session_detail", "b", workout_id=42)], "tool_use"),
        _resp([_text("Solid session.")], "end_turn"),
    ])
    run = advisor.converse(client, _Store(), "review it")
    assert [c["tool"] for c in run.calls] == ["context", "session_detail"]
    assert run.calls[1]["input"] == {"workout_id": 42}
    assert run.input_tokens == 30 and run.output_tokens == 15


def test_parallel_tool_calls_in_one_turn_are_all_answered(monkeypatch):
    """Every tool_use block needs a matching tool_result or the API rejects the turn."""
    monkeypatch.setattr(advisor, "_dispatch", lambda s, n, a: {"ok": n})
    client = _Client([
        _resp([_tool("context", "a"), _tool("race_detail", "b")], "tool_use"),
        _resp([_text("done")], "end_turn"),
    ])
    advisor.converse(client, _Store(), "go")
    results = client.requests[1]["messages"][-1]["content"]
    assert [r["tool_use_id"] for r in results] == ["a", "b"]


def test_a_run_that_never_concludes_stores_nothing(monkeypatch):
    """Better to fail than to persist a half-formed opinion."""
    monkeypatch.setattr(advisor, "_dispatch", lambda s, n, a: {"ok": n})
    client = _Client([_resp([_tool("context", f"t{i}")], "tool_use")
                      for i in range(advisor.MAX_TURNS)])
    with pytest.raises(RuntimeError, match="gave up"):
        advisor.converse(client, _Store(), "go")


def test_an_empty_answer_is_an_error_not_an_empty_review():
    """A blank review stored is a session that looks reviewed and says nothing."""
    client = _Client([_resp([], "end_turn")])
    with pytest.raises(RuntimeError, match="no text"):
        advisor.converse(client, _Store(), "go")


# --- tool dispatch -----------------------------------------------------------

def test_a_bad_argument_is_returned_to_the_model_not_raised():
    """The model can correct a malformed date; a raised error ends the run."""
    out = advisor._dispatch(_Store(), "session_detail", {"workout_id": "not-a-number"})
    assert "error" in out


def test_an_unknown_tool_is_reported():
    assert "unknown tool" in advisor._dispatch(_Store(), "drop_tables", {})["error"]


# --- basis -------------------------------------------------------------------

def test_basis_records_the_coverage_and_the_bands():
    """Both are needed to re-read the review later.

    Without the coverage instant, "no swimming" cannot be distinguished from
    "the record stops here"; without the bands, no zone figure in it means
    anything.
    """
    run = advisor.Run("text", [{"tool": "context", "input": {}}])
    basis = run.basis(_Store())
    assert basis["observed_through"].startswith("2026-08-26")
    assert "Z3 160-169" in basis["zone_bands"]
    assert basis["tool_calls"] == [{"tool": "context", "input": {}}]
    assert basis["model"] == advisor.MODEL


# --- finding work ------------------------------------------------------------

def test_older_unreviewed_sessions_are_counted_not_hidden():
    """A run that quietly skips work looks exactly like one with none to do."""
    store = _Store([[{"id": 1, "activity": "Running",
                      "started_at": datetime(2026, 8, 25, tzinfo=timezone.utc)}],
                    {"n": 2731}])
    rows, older = advisor.unreviewed(store, date(2026, 8, 13), 10)
    assert len(rows) == 1 and older == 2731


def test_a_review_replaces_rather_than_duplicates():
    """Re-running after a fix should correct the review, not add a second."""
    store = _Store()
    advisor.store_review(store, 42, advisor.Run("better take", []))
    sql, _ = store.cur.executed[0]
    assert "ON CONFLICT (workout_id) DO UPDATE" in sql
    assert store.committed


def test_the_plan_carries_where_it_came_from():
    store = _Store()
    advisor.store_plan(store, advisor.Run("## Where you are\nFine.", [{"tool": "context"}]))
    _sql, params = store.cur.executed[0]
    body = params[1]
    assert "Where you are" in body
    assert "covered through 2026-08-26" in body
    assert advisor.MODEL in body


def test_a_truncated_answer_is_not_stored_as_a_finished_one():
    """max_tokens mid-sentence must fail, not persist a review that stops dead."""
    client = _Client([_resp([_text("The session started well and then")], "max_tokens")])
    with pytest.raises(RuntimeError, match="max_tokens"):
        advisor.converse(client, _Store(), "go")


# --- credentials -------------------------------------------------------------

def test_an_oauth_token_is_sent_as_a_bearer_not_an_api_key(monkeypatch):
    """Passing an OAuth token as api_key returns 401 'API key is invalid'.

    That reads like a bad secret rather than the wrong header, and cost a
    diagnosis once already.
    """
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-oat01-abc")
    assert advisor.credential() == {"auth_token": "sk-ant-oat01-abc"}


def test_an_api_key_is_sent_as_an_api_key(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-api03-abc")
    assert advisor.credential() == {"api_key": "sk-ant-api03-abc"}


def test_a_missing_credential_says_where_to_put_one(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    monkeypatch.setattr(advisor, "_KEY_FILE", advisor.Path("/nonexistent/key"))
    with pytest.raises(SystemExit, match="No credential"):
        advisor.credential()
