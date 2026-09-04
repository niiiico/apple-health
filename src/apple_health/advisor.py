"""The advisor: read the record through `queries`, write an opinion about it.

ADR-006 corollary (e). It writes rather than chats, and that is the whole shape:
writing needs only outbound egress, so nothing has to be exposed inbound; the
output is readable from anywhere without the reader reaching this service; and
an opinion on disk can be checked later against the data it was formed from,
which one in a chat window cannot.

Two jobs, at two horizons:

- ``review`` — one session at a time, for sessions with no review yet.
- ``plan`` — a standing document, rewritten from the goals and recent reviews.

The second is built on the first. Reviews are the raw material; the plan is what
they add up to.

**It drives the Claude Code CLI, not the Messages API.** That is the house
arrangement — biblio and braid do the same — and it is a subscription rather
than metered API billing. `CLAUDE_CODE_OAUTH_TOKEN` authenticates it;
`ANTHROPIC_API_KEY` is deliberately *not* set, since its presence would make the
CLI bill the API instead. (An OAuth token sent to the
Messages API directly is refused with a 429 that carries no rate-limit headers —
it reads exactly like throttling and is not. That cost a wrong diagnosis before
the CLI was used, and is the reason `token()` refuses an `sk-ant-api…` key
outright rather than letting the billing change quietly.)

**The model gets one tool: `ah-query`.** `--allowed-tools` permits that command
and nothing else, so it cannot read files, write anything, or run arbitrary
shell. Every write in this module is done by the driver, from the text the model
returned, so a confabulation cannot become a stored fact in one step.

**Everything it sees comes through `queries`**, never raw SQL. Those responses
carry their own coverage boundary and zone bands, so the model cannot read "no
swimming in four weeks" without also being told how far the record is known to
extend. `ah-query` logs each call to `AH_QUERY_LOG`, and that log becomes
`session_reviews.basis` — read back from what actually ran rather than parsed
out of the CLI's event stream.

Usage::

    uv run ah-advise review [--limit N] [--since YYYY-MM-DD] [--dry-run]
    uv run ah-advise plan [--dry-run]
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from . import queries
from .derive.zones import ZONES
from .store import Store

MODEL = "claude-opus-5"
_OAUTH_VAR = "CLAUDE_CODE_OAUTH_TOKEN"
_KEY_FILE = Path.home() / ".config/apple-health/claude-token"
CLI = os.environ.get("AH_CLAUDE_CLI", "claude")

# The only two commands the model may run. Prefix rules, so `ah-query context`
# and `ah-write note --id …` are permitted and nothing else is — no Read, no
# file Write, no bare Bash.
#
# `ah-write` reaches notes, goals and documents only: the things a person types
# by hand. It has no subcommand for `workouts`, `hr_samples`, `laps` or
# `route_points`, and must never grow one — what the watch measured is not a
# model's to edit, and every guarantee this project makes rests on that.
ALLOWED_TOOLS = "Bash(ah-query:*),Bash(ah-write:*)"

# How far back `review` looks for unreviewed sessions by default. Not a filter
# on what is worth reviewing — every session in the window gets one, however
# short — but a bound on how much work one run does. Sessions older than this
# are counted and reported rather than silently skipped.
DEFAULT_REVIEW_DAYS = 14

PLAN_SLUG = "plan"


def token() -> str:
    """The Claude Code subscription token, from the environment or a 0600 file.

    Produced by ``claude setup-token``. Not an API key: an ``sk-ant-api…`` key
    belongs to the Messages API and would be billed per token, which is why the
    CLI is run with ``ANTHROPIC_API_KEY`` scrubbed from its environment.
    """
    value = os.environ.get(_OAUTH_VAR) or ""
    if not value:
        try:
            value = _KEY_FILE.read_text().strip()
        except OSError:
            value = ""
    if not value:
        raise SystemExit(
            f"No token. Set {_OAUTH_VAR} or write one to {_KEY_FILE} (mode 0600).\n"
            f"Produce it with: claude setup-token")
    if value.startswith("sk-ant-api"):
        raise SystemExit(
            "That is an API key, not a Claude Code token. This advisor drives "
            "the CLI on a subscription; an API key here would bill the Messages "
            "API instead. Produce a token with: claude setup-token")
    return value


# --- the one tool ------------------------------------------------------------

TOOLS_DOC = """You have exactly one tool: the `ah-query` command, via Bash. It prints JSON. Nothing else is permitted — no reading files, no other commands.

  ah-query context
      Coverage boundary, heart-rate zone bands, the athlete's goals in their own
      words, and period notes explaining gaps. Run this first, always.

  ah-query sessions --start YYYY-MM-DD --end YYYY-MM-DD [--activity Running]
      Every session in the window, most recent first. No distance floor and no
      filtering by interest: a 9-minute walk is returned beside a two-hour ride.

  ah-query session --id N
      One session in full: zone durations and shares, drift across thirds, laps
      if recorded, route splits, and the athlete's own note. Says so explicitly
      when a session has no heart-rate series, rather than reporting zeroes.

  ah-query metric --metric RestingHeartRate --start YYYY-MM-DD --end YYYY-MM-DD
                  [--bucket week|month]
      One metric over time. Reports which source it read and how far that source
      covers, and warns when it stops short of the window asked for. A short
      answer means the data stops, not that the value went to zero.
      Try: RestingHeartRate, HeartRateVariabilitySDNN, VO2Max, BodyMass.

  ah-query race [--race SLUG]
      Archived per-leg race breakdowns. With no argument, lists what exists.
      The only place official splits and placings live.

  ah-query reviews --start YYYY-MM-DD --end YYYY-MM-DD
      Reviews already written for sessions in that window, so you do not repeat
      last week's point as though it were new. These are prior *opinions*, not
      observations: anything factual in them must be re-queried before you rely
      on it. They may have been wrong.

  ah-write note --id N --text "..."
  ah-write goal --text "..." [--target-date YYYY-MM-DD]
  ah-write doc --slug SLUG --text "..." [--append]
  ah-write memory --text "..."
  ah-write forget --id N
      Your own durable memory, returned by `ah-query context`.

      For what outlives a conversation and should not be re-derived: that a pool
      is 25 m, that a course is hard and not a fitness yardstick, that a
      particular pain recurs under load. Not for an opinion about one session —
      that is a review — and not for what he told you about himself, which is
      his profile and his to edit.

      Record sparingly and specifically. A memory that is really a guess will be
      read as fact by every later conversation, including by you.

  ah-write changes [--limit N]
      Writing. These reach only what the athlete would type himself — his note
      on a session, a goal, a document such as the race plan. There is no way to
      alter what the watch recorded, and you should not look for one.

      Every write is logged with what it replaced, and shown to him afterwards.

      **Ask before you write when the write destroys something.** Use your
      judgement rather than a rule; the distinction that matters is whether
      anything of his is lost:

        - Just do it: adding a note where there is none, recording a goal he has
          just described, `--append`ing to a document, writing a document that
          does not exist yet.
        - Ask first, in the same reply, and wait for him to say yes: replacing a
          note he wrote, rewriting a document wholesale rather than appending,
          anything you are not sure he asked for.

      When you are unsure, propose the exact text and ask. A change he did not
      expect is worse than a question he did not need.

  ah-query doc [--slug SLUG]
      The athlete's own written material. With no argument, lists what exists.
      Two kinds, and they carry different authority:

      * The race PLAN (`kujukuri-2026-plan`) is what was DECIDED — phases, week
        templates, per-leg targets, fixed rest days. Authoritative for intent.
        It cannot tell you what actually happened; only the record can.

      * The LOG (`kujukuri-2026-log`) is a PREVIOUS ANALYSIS of the same record,
        written by hand from a partial view of it. Its factual claims are claims,
        not observations. Anything in it you can re-derive, re-derive — you have
        the complete record and it did not.

      That is not caution for its own sake. The log itself records that a week
      was written up as "vélo = 0" when a 48.5 km ride had happened, because the
      snapshot behind it stopped two days early. It also notes that sessions
      under its capture thresholds (swim <2 km, run <10 km, ride <30 km) were
      sometimes known only by conversation. Your queries have no such floor and
      no such boundary, so where the log and the record disagree the record
      usually wins — and saying so is one of the more useful things you can do.

      Both remain worth reading: the plan is the only source for intent, and the
      log is the only source for why a week went the way it did."""


SYSTEM = """You are advising one endurance athlete on their own training, reading \
their real training record through the tools provided.

Write for them directly, in the second person.

`ah-query context` gives you who they are — age, background, how they think about \
training, and any health constraint. **Read it as a person, not a header.** \
Twenty-three years of endurance training, a diagnosis, a comeback, a course they \
know is hard: these change what a number means. A hip that hurts under load in \
someone with a rheumatological history is not the same fact as a niggle, and \
advice that ignores that is not neutral — it is wrong.

Be concrete, and be brief when brevity serves. Earlier instructions here said to \
skip encouragement entirely, and that overshot: it produced advice that read like \
a report on a stranger. Do not pad, do not congratulate reflexively, and do not \
soften a real problem. But you are talking to someone about the thing he has \
organised his week around for two decades — write like it. Where the work has \
been good, say so plainly, because a plain observation from someone reading the \
whole record is worth something and false neutrality is its own distortion.

Rules that matter more than being helpful:

1. Run `ah-query context` before your first answer in a conversation. It gives you \
the coverage boundary, the zone bands and their goals, and numbers from the other \
tools mean nothing without it.

   Do **not** run it again later in the same conversation unless something has \
changed that matters — you have moved to a different period, or the answer turns \
on how far the record extends. A follow-up about data you already fetched, or a \
question that does not touch the record at all (what a drill is, what a term \
means), needs no query whatsoever. Re-fetching what you already have costs him a \
minute of waiting and tells him nothing.

2. Absence of data is never evidence of absence of training. If you see no \
swimming for four weeks, the possibilities are: they did not swim, the pool was \
shut, the sync is broken, or the window runs past the coverage boundary. Check \
`context` for a period note explaining it, and if there is none, say what you \
observed and that you cannot tell which — never "you have stopped swimming".

3. Reuse what you already fetched in this conversation rather than fetching it \
again. The record does not change while you are answering.

4. Never report a number the tools did not give you. Do not estimate a pace you \
were not shown, do not infer a heart rate from a distance, do not carry a figure \
over from your own knowledge of typical athletes. If you want a number, query \
for it.

5. Zone figures come from one fixed set of bands, given to you by `ah-query context`. \
They are not read from the watch — nothing can read them from the watch — so if \
they look wrong for this athlete, say so rather than silently reinterpreting them.

6. If the data does not support a conclusion, say that. "The record does not show \
enough to tell" is a useful answer here and a wrong answer is not. You are not \
being scored on having an opinion.

7. **Write in French.** The athlete is French, their goals, race plan and \
training log are in French, and what you write sits alongside them. Use the \
vocabulary those documents already use — natation, vélo, course, brick, seuil, \
allure, FC, dérive, affûtage — rather than translating it. Zone labels (Z1…Z5) \
and metric names stay as the tools report them."""

REVIEW_TASK = SYSTEM + "\n\n" + TOOLS_DOC + """

Review this single session: workout id {workout_id}.

Run `ah-query session --id {workout_id}`, and whatever else you need to judge it \
— the sessions around it, a metric trend, a past race, the race plan, what you \
already wrote about recent sessions. Then write the review.

Check the plan (`ah-query doc`) for what this session was *supposed* to be. A \
session that matched the plan and a session that replaced it are different \
things to report, and only the plan can tell you which happened.

Judge the session from the record, not from what the log concluded about that \
week. If the log made a claim you can check, check it.

Length should match what happened. An easy half-hour walk deserves one sentence. \
A hard interval session or a long ride deserves a short paragraph. Do not pad a \
quiet session into a paragraph, and do not compress a significant one into a line.

Cover, only where there is something to say: what the session was, how it went \
against the athlete's goals, anything notable in the zone distribution or drift, \
and anything that should change what they do next. If a session is unremarkable, \
saying so is the correct review.

Write only the review text. No preamble, no heading, no sign-off."""

PLAN_TASK = SYSTEM + "\n\n" + TOOLS_DOC + """

Write the athlete's standing training plan, as of {today}.

Start with `ah-query context` to read their goals. Everything you write should serve those \
goals; if none are recorded, say plainly that no goal is recorded and that the \
plan is therefore a description of current training rather than a plan towards \
anything — do not invent an objective.

Read the race plan and the training log with `ah-query doc` — they carry the \
phase structure, the week templates and the per-leg targets, and the plan you \
write continues them rather than starting over. Look at recent training with \
`ah-query sessions`, at what you already concluded with `ah-query reviews`, at \
whatever trends matter with `ah-query metric`, and at past races with \
`ah-query race`.

Write in markdown, and keep it to something readable on a phone before a race. \
Suggested shape, to vary as the situation deserves:

- **Où tu en es** — an honest read of current training against the phase the \
plan says you are in, including what the record cannot tell you.
- **Les prochaines semaines** — what to do, concretely enough to act on, \
respecting the fixed constraints in the plan (rest days, session structure).
- **À surveiller** — anything in the data that warrants attention.

This document is rewritten in full each time, so it should read as current on its \
own, without reference to previous versions."""


CHAT_TASK = SYSTEM + "\n\n" + TOOLS_DOC + """

The athlete is asking you something directly. Answer it.

This is a conversation, not a report: answer what was asked, at the length the \
question deserves, and stop. No headings unless the answer genuinely needs them.

Query for anything factual — you have the whole record, and guessing at a number \
you could have looked up is the one unforgivable move here. If the answer depends \
on something the record cannot tell you, say which part and why.

Their question:

{message}"""


def chat(message: str, session_id: str | None = None, timeout: float = 600.0,
         on_progress: Any = None, history: list[dict] | None = None) -> Run:
    """One conversational turn, continuing `session_id` when given.

    The first turn carries the full instructions; a resumed one carries only the
    question, because the CLI already has the rest in its transcript.

    **Resuming can fail, and must not be fatal.** The CLI keeps sessions under
    its own `$HOME` inside the pod, so a restart loses every one of them while
    the transcript survives in Postgres. Rather than tell someone their
    conversation is gone when the words are plainly on the screen, a failed
    resume replays the stored history as context and starts a new session.
    """
    if session_id:
        try:
            return run_streaming(message, timeout=timeout, resume=session_id,
                                 on_progress=on_progress)
        except RuntimeError:
            pass                      # session gone with the pod; rebuild below

    task = CHAT_TASK.format(message=message)
    if session_id and history:
        task = (CHAT_TASK.split("Their question:")[0]
                + "This conversation is already under way. What was said so far:\n\n"
                + "\n\n".join(f"Them: {h['question']}\nYou: {h['answer']}"
                                for h in history[-8:])
                + f"\n\nTheir question:\n\n{message}")
    return run_streaming(task, timeout=timeout, on_progress=on_progress)


@dataclass
class Run:
    """One completed conversation: the text produced, and what it looked at."""

    text: str
    calls: list[dict[str, Any]] = field(default_factory=list)
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float | None = None
    turns: int | None = None
    session_id: str | None = None
    steps: list[dict[str, str]] = field(default_factory=list)

    def basis(self, store: Store) -> dict[str, Any]:
        """What the opinion was formed from, for storing beside it.

        A review is an opinion about data. Without the data it saw, it can be
        neither checked nor re-run, and in a year it is indistinguishable from a
        guess.
        """
        cov = store.coverage()
        return {
            "model": MODEL,
            "observed_through": (cov.observed_through.isoformat()
                                 if cov.observed_through else None),
            "zone_bands": {label: f"{lo}-{hi}" for label, lo, hi in ZONES},
            "queries": self.calls,
            "usage": {"input": self.input_tokens, "output": self.output_tokens,
                      "cost_usd": self.cost_usd, "turns": self.turns},
        }


def run_streaming(task: str, timeout: float = 900.0, resume: str | None = None,
                  on_progress: Any = None) -> Run:
    """Run the CLI in streaming mode, reporting progress as it arrives.

    `--output-format stream-json` emits newline-delimited events as each
    assistant message completes — not token by token. So what a reader sees is
    each query being run and then the answer, which is honest progress rather
    than a typing effect.

    `on_progress(text, queries)` is called as those arrive. It is what makes a
    two-minute answer visible while it is still being formed; the alternative is
    a spinner that says nothing about whether anything is happening.
    """
    env = dict(os.environ)
    env.pop("ANTHROPIC_API_KEY", None)
    env[_OAUTH_VAR] = token()

    with tempfile.TemporaryDirectory(prefix="ah-advise-") as work:
        log = Path(work) / "queries.jsonl"
        env["AH_QUERY_LOG"] = str(log)
        if resume:
            env["AH_WRITE_SESSION"] = resume
        argv = [CLI, "-p", task,
                "--allowed-tools", ALLOWED_TOOLS,
                "--output-format", "stream-json", "--verbose",
                "--model", MODEL]
        if resume:
            argv += ["--resume", resume]

        try:
            proc = subprocess.Popen(argv, cwd=work, env=env,
                                    stdin=subprocess.DEVNULL,
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                    text=True, bufsize=1)
        except FileNotFoundError:
            raise RuntimeError(
                f"{CLI!r} is not installed. The advisor drives the Claude Code "
                f"CLI; install it with: curl -fsSL https://claude.ai/install.sh | bash"
            ) from None

        text_parts: list[str] = []
        queries: list[str] = []
        # A readable trace of what it is doing, not just which tools fired. Each
        # Bash call carries the model's own one-line description of its intent
        # alongside the command, which is the most informative thing the stream
        # offers — thinking blocks are not emitted at all.
        steps: list[dict[str, str]] = []
        envelope: dict[str, Any] | None = None
        session_id: str | None = None
        deadline = time.monotonic() + timeout

        try:
            for line in proc.stdout:
                if time.monotonic() > deadline:
                    proc.kill()
                    raise RuntimeError(f"gave up after {timeout:.0f}s; nothing stored")
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                kind = event.get("type")
                if kind == "system" and event.get("session_id"):
                    session_id = event["session_id"]
                elif kind == "assistant":
                    for block in (event.get("message") or {}).get("content") or []:
                        btype = block.get("type")
                        if btype == "text" and block.get("text"):
                            text_parts.append(block["text"])
                            steps.append({"kind": "text", "text": block["text"]})
                        elif btype == "thinking" and block.get("thinking"):
                            # Not emitted today, but costs nothing to carry and
                            # means this keeps working if that changes.
                            steps.append({"kind": "thinking",
                                          "text": block["thinking"]})
                        elif btype == "tool_use":
                            args = block.get("input") or {}
                            cmd = args.get("command", "")
                            queries.append(cmd.split()[1] if len(cmd.split()) > 1
                                           else block.get("name", "?"))
                            steps.append({
                                "kind": "tool",
                                "text": args.get("description") or block.get("name", "?"),
                                "detail": cmd,
                            })
                    if on_progress:
                        on_progress("\n\n".join(text_parts), list(queries),
                                    list(steps))
                elif kind == "result":
                    envelope = event
                    session_id = event.get("session_id") or session_id
            proc.wait(timeout=30)
        finally:
            if proc.poll() is None:
                proc.kill()

        calls = _read_log(log)

    if envelope is None:
        detail = (proc.stderr.read() or "").strip()[:400] if proc.stderr else ""
        raise RuntimeError(f"the CLI produced no result event. {detail}".strip())
    if envelope.get("is_error"):
        raise RuntimeError(
            f"claude reported an error: {envelope.get('subtype') or 'unknown'}")

    text = (envelope.get("result") or "\n\n".join(text_parts)).strip()
    if not text:
        raise RuntimeError("the model returned no text; nothing stored")

    usage = envelope.get("usage") or {}
    return Run(text, calls,
               input_tokens=usage.get("input_tokens", 0),
               output_tokens=usage.get("output_tokens", 0),
               cost_usd=envelope.get("total_cost_usd"),
               turns=envelope.get("num_turns"),
               session_id=session_id, steps=steps)


def run_claude(task: str, timeout: float = 900.0, resume: str | None = None) -> Run:
    """Run one Claude Code invocation and return its text and what it queried.

    `--allowed-tools` is the security boundary: the model may run `ah-query` and
    nothing else. `--output-format json` gives a parseable envelope rather than
    prose that has to be scraped.

    ANTHROPIC_API_KEY is removed from the child's environment. If it is set, the
    CLI authenticates against the metered API instead of the subscription — the
    run would work and the billing would silently change, which is the kind of
    difference nobody notices until an invoice.
    """
    env = dict(os.environ)
    env.pop("ANTHROPIC_API_KEY", None)
    env[_OAUTH_VAR] = token()

    with tempfile.TemporaryDirectory(prefix="ah-advise-") as work:
        log = Path(work) / "queries.jsonl"
        env["AH_QUERY_LOG"] = str(log)
        try:
            argv = [CLI, "-p", task,
                    "--allowed-tools", ALLOWED_TOOLS,
                    "--output-format", "json",
                    "--model", MODEL]
            if resume:
                # Continues an existing conversation. The transcript lives in
                # the CLI's own state under $HOME, so it does not survive a pod
                # restart — chat history is deliberately ephemeral, while
                # anything worth keeping is written to the database instead.
                argv += ["--resume", resume]
            proc = subprocess.run(
                argv, cwd=work, env=env, stdin=subprocess.DEVNULL,
                capture_output=True, text=True, timeout=timeout)
        except FileNotFoundError:
            raise RuntimeError(
                f"{CLI!r} is not installed. The advisor drives the Claude Code "
                f"CLI; install it with: curl -fsSL https://claude.ai/install.sh | bash"
            ) from None
        except subprocess.TimeoutExpired:
            raise RuntimeError(f"gave up after {timeout:.0f}s; nothing stored") from None

        calls = _read_log(log)

    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip()[:400]
        raise RuntimeError(f"claude exited {proc.returncode}: {detail}")

    envelope = _envelope(proc.stdout)
    text = (envelope.get("result") or "").strip()
    if not text:
        # A blank review stored is a session that looks reviewed and says nothing.
        raise RuntimeError("the model returned no text; nothing stored")

    usage = envelope.get("usage") or {}
    return Run(text, calls,
               input_tokens=usage.get("input_tokens", 0),
               output_tokens=usage.get("output_tokens", 0),
               cost_usd=envelope.get("total_cost_usd"),
               turns=envelope.get("num_turns"),
               session_id=envelope.get("session_id"))


def _envelope(stdout: str) -> dict[str, Any]:
    """The run's result envelope, out of the CLI's event list.

    ``--output-format json`` emits a *list* of events, not one object; the last
    of type ``result`` carries the answer, the turn count and the cost. Reading
    the list as an object fails with "'list' object has no attribute 'get'",
    which says nothing about what was actually wrong.
    """
    try:
        events = json.loads(stdout)
    except json.JSONDecodeError:
        raise RuntimeError(
            f"could not parse the CLI output: {stdout[:200]!r}") from None

    if isinstance(events, dict):                  # tolerated: a single envelope
        events = [events]
    results = [e for e in events
               if isinstance(e, dict) and e.get("type") == "result"]
    if not results:
        raise RuntimeError("the CLI returned no result event")

    envelope = results[-1]
    if envelope.get("is_error"):
        raise RuntimeError(
            f"claude reported an error: {envelope.get('subtype') or 'unknown'}")
    return envelope


def _read_log(path: Path) -> list[dict[str, Any]]:
    """The queries that actually ran, in order.

    Read from what executed rather than from the CLI's event stream: the stream
    is someone else's format and reports what was *requested*.
    """
    try:
        lines = path.read_text().splitlines()
    except OSError:
        return []
    out = []
    for line in lines:
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def unreviewed(store: Store, since: date, limit: int) -> tuple[list[dict], int]:
    """Sessions in [since, now] with no review, plus how many older ones exist.

    The older count is returned rather than swallowed: a run that quietly skips
    work looks identical to a run that had none to do.
    """
    with store.cursor() as cur:
        cur.execute(
            """SELECT w.id, w.activity, w.started_at
                 FROM workouts w
            LEFT JOIN session_reviews r ON r.workout_id = w.id
                WHERE r.workout_id IS NULL AND w.started_at >= %s
             ORDER BY w.started_at DESC
                LIMIT %s""",
            (since, limit))
        rows = [dict(r) for r in cur.fetchall()]
        cur.execute(
            """SELECT count(*) n
                 FROM workouts w
            LEFT JOIN session_reviews r ON r.workout_id = w.id
                WHERE r.workout_id IS NULL AND w.started_at < %s""",
            (since,))
        older = cur.fetchone()["n"]
    return rows, older


def store_review(store: Store, workout_id: int, run: Run) -> None:
    """Persist one review. Idempotent on workout_id, so a re-run replaces it."""
    with store.cursor() as cur:
        cur.execute(
            """INSERT INTO session_reviews (workout_id, review, model, basis)
               VALUES (%s,%s,%s,%s)
               ON CONFLICT (workout_id) DO UPDATE SET
                   review = excluded.review, model = excluded.model,
                   basis = excluded.basis, created_at = now()""",
            (workout_id, run.text, MODEL, json.dumps(run.basis(store))))
    store.commit()


def store_plan(store: Store, run: Run) -> None:
    """Persist the plan, replacing the previous one."""
    body = run.text + "\n\n---\n\n_" + _provenance(run, store) + "_\n"
    with store.cursor() as cur:
        cur.execute(
            """INSERT INTO documents (slug, body, volatility, updated_at)
               VALUES (%s,%s,'high',now())
               ON CONFLICT (slug) DO UPDATE SET
                   body = excluded.body, updated_at = now()""",
            (PLAN_SLUG, body))
    store.commit()


def _provenance(run: Run, store: Store) -> str:
    """A one-line footer saying what the plan was written from."""
    basis = run.basis(store)
    return (f"Written {datetime.now().astimezone():%Y-%m-%d %H:%M} by {MODEL} from a "
            f"record covered through {basis['observed_through']}, "
            f"using {len(run.calls)} queries.")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Write session reviews and the standing plan.")
    sub = ap.add_subparsers(dest="command", required=True)

    rev = sub.add_parser("review", help="review sessions that have none")
    rev.add_argument("--limit", type=int, default=10)
    rev.add_argument("--since", type=date.fromisoformat,
                     default=date.today() - timedelta(days=DEFAULT_REVIEW_DAYS))
    rev.add_argument("--dry-run", action="store_true", help="print, store nothing")

    pl = sub.add_parser("plan", help="rewrite the standing plan")
    pl.add_argument("--dry-run", action="store_true", help="print, store nothing")

    args = ap.parse_args(argv)

    token()          # fail before opening a connection or doing any work
    store = Store(None)
    try:
        return _review(store, args) if args.command == "review" else _plan(store, args)
    finally:
        store.close()


def _review(store: Store, args: argparse.Namespace) -> int:
    rows, older = unreviewed(store, args.since, args.limit)
    if older:
        print(f"note: {older} unreviewed session(s) before {args.since} were not "
              f"touched — pass --since to reach them", file=sys.stderr)
    if not rows:
        print("nothing to review")
        return 0

    failures = 0
    for row in rows:
        label = f"{row['started_at']:%Y-%m-%d %H:%M} {row['activity']} (#{row['id']})"
        try:
            run = run_claude(REVIEW_TASK.format(workout_id=row["id"]))
        except Exception as exc:                  # noqa: BLE001
            # One bad session must not abandon the rest; the review is simply
            # not stored, so the next run tries it again.
            print(f"FAILED {label}: {exc}", file=sys.stderr)
            failures += 1
            continue
        if args.dry_run:
            print(f"\n=== {label}\n{run.text}")
        else:
            store_review(store, row["id"], run)
            print(f"reviewed {label}  ({len(run.calls)} queries"
                  + (f", ${run.cost_usd:.3f}" if run.cost_usd else "") + ")")
    return 1 if failures else 0


def _plan(store: Store, args: argparse.Namespace) -> int:
    try:
        run = run_claude(PLAN_TASK.format(today=date.today().isoformat()))
    except Exception as exc:                      # noqa: BLE001
        print(f"FAILED: {exc}", file=sys.stderr)
        return 1
    if args.dry_run:
        print(run.text)
        return 0
    store_plan(store, run)
    print(f"plan written ({len(run.calls)} queries"
          + (f", ${run.cost_usd:.3f}" if run.cost_usd else "") + ")")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
