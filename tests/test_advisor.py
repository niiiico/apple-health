"""Tests for the advisor's driver and its bookkeeping.

The advisor shells out to the Claude Code CLI, so the interesting tests run a
*fake* CLI — a small script that writes an envelope and a query log. That
exercises the real subprocess path, the real flags and the real parsing, which a
mocked `subprocess.run` would not.
"""

from __future__ import annotations

import json
import stat
from datetime import date, datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from apple_health import advisor


# --- doubles -----------------------------------------------------------------

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


def _fake_cli(tmp_path: Path, body: str) -> Path:
    """A stand-in for `claude` that records its argv and environment."""
    script = tmp_path / "fake-claude"
    script.write_text("#!/bin/sh\n"
                      f'printf "%s\\n" "$*" > {tmp_path}/argv\n'
                      f'env > {tmp_path}/env\n'
                      f"{body}\n")
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    return script


def _envelope(result: str, **extra) -> str:
    """The CLI emits a *list* of events; the last `result` one is the envelope."""
    events = [
        {"type": "system", "subtype": "init", "model": "claude-opus-5"},
        {"type": "result", "subtype": "success", "result": result, "is_error": False,
         "usage": {"input_tokens": 100, "output_tokens": 20},
         "total_cost_usd": 0.012, "num_turns": 3, **extra},
    ]
    return f"cat <<'JSON'\n{json.dumps(events)}\nJSON"


# --- the token ---------------------------------------------------------------

def test_an_api_key_is_refused_rather_than_silently_billed(monkeypatch):
    """An sk-ant-api key here would work, and change the billing.

    The CLI accepts one and authenticates against the metered API instead of the
    subscription. The run succeeds, so nothing surfaces until an invoice.
    """
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "sk-ant-api03-nope")
    with pytest.raises(SystemExit, match="API key, not a Claude Code token"):
        advisor.token()


def test_an_oauth_token_is_accepted(monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "sk-ant-oat01-yes")
    assert advisor.token() == "sk-ant-oat01-yes"


def test_a_missing_token_says_how_to_make_one(monkeypatch, tmp_path):
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    monkeypatch.setattr(advisor, "_KEY_FILE", tmp_path / "absent")
    with pytest.raises(SystemExit, match="claude setup-token"):
        advisor.token()


# --- running the CLI ---------------------------------------------------------

def test_the_model_is_confined_to_ah_query(monkeypatch, tmp_path):
    """The allowed-tools flag is the security boundary, not a preference.

    Without it the model could read files and run arbitrary shell in a process
    holding a database password.
    """
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "sk-ant-oat01-x")
    monkeypatch.setattr(advisor, "CLI", str(_fake_cli(tmp_path, _envelope("fine"))))
    advisor.run_claude("go")
    argv = (tmp_path / "argv").read_text()
    assert "--allowed-tools Bash(ah-query:*)" in argv
    assert "--output-format json" in argv


def test_an_api_key_is_scrubbed_from_the_child(monkeypatch, tmp_path):
    """If ANTHROPIC_API_KEY is set, the CLI bills the API instead of the plan."""
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "sk-ant-oat01-x")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-api03-leak")
    monkeypatch.setattr(advisor, "CLI", str(_fake_cli(tmp_path, _envelope("fine"))))
    advisor.run_claude("go")
    child_env = (tmp_path / "env").read_text()
    assert "ANTHROPIC_API_KEY" not in child_env
    assert "CLAUDE_CODE_OAUTH_TOKEN" in child_env


def test_the_queries_that_ran_are_recorded(monkeypatch, tmp_path):
    """`basis` is read from what executed, not from the CLI's event stream."""
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "sk-ant-oat01-x")
    body = ('printf \'{"query":"context","args":{}}\\n\' >> "$AH_QUERY_LOG"\n'
            'printf \'{"query":"session","args":{"id":42}}\\n\' >> "$AH_QUERY_LOG"\n'
            + _envelope("Solid session."))
    monkeypatch.setattr(advisor, "CLI", str(_fake_cli(tmp_path, body)))
    run = advisor.run_claude("go")
    assert run.text == "Solid session."
    assert [c["query"] for c in run.calls] == ["context", "session"]
    assert run.calls[1]["args"] == {"id": 42}
    assert run.cost_usd == 0.012 and run.turns == 3


def test_a_nonzero_exit_stores_nothing(monkeypatch, tmp_path):
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "sk-ant-oat01-x")
    monkeypatch.setattr(advisor, "CLI",
                        str(_fake_cli(tmp_path, 'echo "usage limit reached" >&2\nexit 1')))
    with pytest.raises(RuntimeError, match="exited 1"):
        advisor.run_claude("go")


def test_an_empty_result_is_an_error_not_a_blank_review(monkeypatch, tmp_path):
    """A blank review stored is a session that looks reviewed and says nothing."""
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "sk-ant-oat01-x")
    monkeypatch.setattr(advisor, "CLI", str(_fake_cli(tmp_path, _envelope("   "))))
    with pytest.raises(RuntimeError, match="no text"):
        advisor.run_claude("go")


def test_an_error_envelope_is_not_stored_as_a_review(monkeypatch, tmp_path):
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "sk-ant-oat01-x")
    monkeypatch.setattr(advisor, "CLI",
                        str(_fake_cli(tmp_path, _envelope("partial", is_error=True))))
    with pytest.raises(RuntimeError, match="reported an error"):
        advisor.run_claude("go")


def test_unparseable_output_is_named_as_such(monkeypatch, tmp_path):
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "sk-ant-oat01-x")
    monkeypatch.setattr(advisor, "CLI", str(_fake_cli(tmp_path, 'echo "not json"')))
    with pytest.raises(RuntimeError, match="could not parse"):
        advisor.run_claude("go")


def test_an_event_list_with_no_result_is_refused(monkeypatch, tmp_path):
    """The CLI emits a list; reading it as an object gave a meaningless error."""
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "sk-ant-oat01-x")
    body = """cat <<'JSON'
[{"type":"system","subtype":"init"}]
JSON"""
    monkeypatch.setattr(advisor, "CLI", str(_fake_cli(tmp_path, body)))
    with pytest.raises(RuntimeError, match="no result event"):
        advisor.run_claude("go")


def test_a_missing_cli_says_how_to_install_it(monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "sk-ant-oat01-x")
    monkeypatch.setattr(advisor, "CLI", "/nonexistent/claude")
    with pytest.raises(RuntimeError, match="install.sh"):
        advisor.run_claude("go")


def test_a_corrupt_log_line_does_not_lose_the_rest(tmp_path):
    log = tmp_path / "q.jsonl"
    log.write_text('{"query":"context","args":{}}\nhalf-written\n'
                   '{"query":"race","args":{}}\n')
    assert [c["query"] for c in advisor._read_log(log)] == ["context", "race"]


# --- basis -------------------------------------------------------------------

def test_basis_records_the_coverage_and_the_bands():
    """Both are needed to re-read a review later.

    Without the coverage instant, "no swimming" cannot be told from "the record
    stops here"; without the bands, no zone figure in it means anything.
    """
    basis = advisor.Run("text", [{"query": "context", "args": {}}]).basis(_Store())
    assert basis["observed_through"].startswith("2026-08-26")
    assert "Z3 160-169" in basis["zone_bands"]
    assert basis["queries"] == [{"query": "context", "args": {}}]
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
    advisor.store_plan(store, advisor.Run("## Where you are\nFine.",
                                          [{"query": "context"}]))
    _sql, params = store.cur.executed[0]
    body = params[1]
    assert "Where you are" in body
    assert "covered through 2026-08-26" in body
    assert advisor.MODEL in body


# --- the prompt --------------------------------------------------------------

def test_the_task_names_the_command_to_run():
    """The tools live in the prompt: a CLI invocation has no tool schema."""
    task = advisor.REVIEW_TASK.format(workout_id=42)
    assert "ah-query session --id 42" in task
    assert "ah-query context" in task
    assert "Absence of data is never evidence of absence of training" in task
