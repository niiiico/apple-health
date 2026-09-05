"""Questions a review asks, and the loop that lets them be answered.

The behaviour under test is a loop, not a table: a review asks something the
record cannot answer, the athlete answers it on the page, and the session goes
back into the review queue so the reading is rewritten knowing it. Before this
existed the question lived in review prose — a document reviewed once and never
rewritten — so answering it changed nothing and the next model asked again. It
went round three times on one question about a hip.
"""

from __future__ import annotations

import pytest

from apple_health import ui


# --- rendering ---------------------------------------------------------------

def test_nothing_is_rendered_when_there_is_nothing_to_ask():
    """An empty heading on every session would train him to skip the section."""
    assert ui.questions_block(None) == ""
    assert ui.questions_block([]) == ""


def test_an_open_question_gets_a_box_to_answer_it():
    html = ui.questions_block([
        {"key": "hanche-droite", "question": "La hanche droite du 27/8 — passée ?",
         "answer": None, "asked_at": "2026-08-27T10:00:00+00:00",
         "answered_at": None, "times_asked": 1}])
    assert "La hanche droite du 27/8" in html
    assert 'data-action="answer_question"' in html
    assert 'data-field="key" value="hanche-droite"' in html


def test_a_repeated_question_says_so():
    """A question on its third asking carries information the first did not."""
    html = ui.questions_block([
        {"key": "hanche-droite", "question": "La hanche ?", "answer": None,
         "asked_at": "2026-08-27T10:00:00+00:00", "answered_at": None,
         "times_asked": 3}])
    assert "reposée 3×" in html


def test_asked_once_does_not_say_how_often():
    html = ui.questions_block([
        {"key": "k", "question": "Q ?", "answer": None,
         "asked_at": "2026-08-27T10:00:00+00:00", "answered_at": None,
         "times_asked": 1}])
    assert "reposée" not in html


def test_an_answered_question_keeps_the_answer_visible():
    """What he said is the part worth keeping, and a question that disappears
    on being answered gives no sign the loop closed."""
    html = ui.questions_block([
        {"key": "hanche-droite", "question": "La hanche ?",
         "answer": "Rien depuis le 30/8.",
         "asked_at": "2026-08-27T10:00:00+00:00",
         "answered_at": "2026-09-05T10:00:00+00:00", "times_asked": 2}])
    assert "Rien depuis le 30/8." in html
    assert "répondu le 2026-09-05" in html
    # No second box: it has been answered.
    assert 'data-action="answer_question"' not in html


def test_open_questions_come_before_answered_ones():
    html = ui.questions_block([
        {"key": "b", "question": "OUVERTE", "answer": None,
         "asked_at": "2026-09-01T10:00:00+00:00", "answered_at": None,
         "times_asked": 1},
        {"key": "a", "question": "FERMEE", "answer": "oui",
         "asked_at": "2026-08-01T10:00:00+00:00",
         "answered_at": "2026-08-02T10:00:00+00:00", "times_asked": 1}])
    assert html.index("OUVERTE") < html.index("FERMEE")


def test_the_count_in_the_heading_is_of_open_ones_only():
    """A heading reading "(3)" over three answered questions is a false alarm."""
    html = ui.questions_block([
        {"key": "a", "question": "Q1", "answer": "oui",
         "asked_at": "2026-08-01T10:00:00+00:00",
         "answered_at": "2026-08-02T10:00:00+00:00", "times_asked": 1},
        {"key": "b", "question": "Q2", "answer": None,
         "asked_at": "2026-09-01T10:00:00+00:00", "answered_at": None,
         "times_asked": 1}], heading="Questions")
    assert "<h2>Questions (1)</h2>" in html


def test_question_text_is_escaped():
    """It is model-written text landing in a page the athlete opens."""
    html = ui.questions_block([
        {"key": "k", "question": "<script>alert(1)</script>", "answer": None,
         "asked_at": "2026-09-01T10:00:00+00:00", "answered_at": None,
         "times_asked": 1}])
    assert "<script>" not in html
    assert "&lt;script&gt;" in html


def test_an_answer_is_escaped_too():
    html = ui.questions_block([
        {"key": "k", "question": "Q", "answer": "<b>non</b>",
         "asked_at": "2026-09-01T10:00:00+00:00",
         "answered_at": "2026-09-02T10:00:00+00:00", "times_asked": 1}])
    assert "<b>non</b>" not in html
    assert "&lt;b&gt;non&lt;/b&gt;" in html


def test_the_session_link_is_only_offered_when_asked_for():
    """On the session page the link would point at the page you are reading."""
    q = [{"key": "k", "question": "Q", "answer": None, "workout_id": 5566,
          "asked_at": "2026-09-01T10:00:00+00:00", "answered_at": None,
          "times_asked": 1}]
    assert "/session/5566" in ui.questions_block(q, link_sessions=True)
    assert "/session/5566" not in ui.questions_block(q)
