"""``ah-write`` — the narrow write surface the advisor is allowed.

Everything here writes something a person would otherwise type by hand: a note
on a session, a goal, a document such as the race plan. **Nothing here can touch
what a sensor measured.** `workouts`, `hr_samples`, `laps`, `route_points` and
`daily_metrics` have no subcommand and never will: the record of what actually
happened comes from the watch, and a model editing it would corrupt the one
thing this project exists to keep honest.

Every write records what it displaced in ``advisor_writes``. That is what makes
a change reviewable rather than a rumour — you can see what it was before, and
there is something to undo it with. A write that cannot be undone is not
offered.

Usage::

    ah-write note --id 5560 --text "…"
    ah-write goal --text "…" [--target-date 2026-10-03]
    ah-write doc --slug kujukuri-2026-plan --text "…" [--append]
    ah-write question --key hanche-droite --text "…" [--id 5560]
    ah-write changes [--limit 10]

``AH_WRITE_SESSION`` ties each write to the conversation that made it, so a
change can be traced back to the exchange that produced it.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import date
from typing import Any

from .store import Store, archive, archive_note

_SESSION_VAR = "AH_WRITE_SESSION"


def _record(cur, target: str, key: str, summary: str,
            before: Any, after: Any) -> None:
    """Log the change and what it displaced."""
    cur.execute(
        """INSERT INTO advisor_writes
               (session_id, target, target_key, summary, before, after)
           VALUES (%s,%s,%s,%s,%s,%s)""",
        (os.environ.get(_SESSION_VAR), target, str(key), summary,
         json.dumps(before) if before is not None else None,
         json.dumps(after) if after is not None else None))


def write_note(store: Store, workout_id: int, text: str) -> str:
    """Set the athlete's note on one session.

    Overwrites, because a session has one note. The previous text is kept in the
    audit row — losing what he wrote himself would be the worst thing on this
    page, and it is the reason `before` exists.
    """
    text = text.strip()
    with store.cursor() as cur:
        cur.execute("SELECT id FROM workouts WHERE id = %s", (workout_id,))
        if cur.fetchone() is None:
            raise SystemExit(f"no session {workout_id}")
        cur.execute("SELECT note FROM session_notes WHERE workout_id = %s",
                    (workout_id,))
        row = cur.fetchone()
        before = row["note"] if row else None
        # Kept as a version as well as in the audit row: the audit says what a
        # given write displaced, the revisions say what the note has been.
        archive_note(cur, workout_id, "advisor")

        if not text:
            cur.execute("DELETE FROM session_notes WHERE workout_id = %s",
                        (workout_id,))
            summary = f"note supprimée sur la séance {workout_id}"
        else:
            cur.execute(
                """INSERT INTO session_notes (workout_id, note)
                   VALUES (%s,%s)
                   ON CONFLICT (workout_id) DO UPDATE SET
                       note = excluded.note, updated_at = now()""",
                (workout_id, text))
            summary = (f"note {'remplacée' if before else 'ajoutée'} sur la "
                       f"séance {workout_id}")
        _record(cur, "session_notes", workout_id, summary, before, text or None)
    store.commit()
    return summary


def write_goal(store: Store, text: str, target_date: date | None,
               goal_id: int | None = None) -> str:
    """Record a goal, or amend one.

    Editing is allowed now, and versioned: a goal whose wording moves is the
    same goal, and archiving one to write another loses the thread of what was
    being aimed at and why it changed.
    """
    text = text.strip()
    if not text:
        raise SystemExit("goal text is required")
    with store.cursor() as cur:
        if goal_id:
            cur.execute("SELECT goal FROM goals WHERE id = %s AND archived_at IS NULL",
                        (goal_id,))
            row = cur.fetchone()
            if row is None:
                raise SystemExit(f"no active goal {goal_id}")
            before = row["goal"]
            archive(cur, "goals", goal_id, "advisor")
            cur.execute("UPDATE goals SET goal = %s, target_date = %s WHERE id = %s",
                        (text, target_date, goal_id))
            summary = f"objectif #{goal_id} modifié"
        else:
            before = None
            cur.execute(
                "INSERT INTO goals (goal, target_date) VALUES (%s,%s) RETURNING id",
                (text, target_date))
            goal_id = cur.fetchone()["id"]
            summary = f"objectif #{goal_id} ajouté"
        _record(cur, "goals", goal_id, summary, before,
                {"goal": text,
                 "target_date": target_date.isoformat() if target_date else None})
    store.commit()
    return summary


def write_doc(store: Store, slug: str, text: str, append: bool) -> str:
    """Write or extend a document.

    `--append` exists because amending a plan is the common case and replacing
    one wholesale is the rare, destructive one. A model asked to "update the
    plan" will otherwise rewrite six thousand words to change a paragraph, and
    the diff will be unreadable at the moment it most needs reading.
    """
    if not slug or not text.strip():
        raise SystemExit("slug and text are required")
    with store.cursor() as cur:
        cur.execute("SELECT body FROM documents WHERE slug = %s", (slug,))
        row = cur.fetchone()
        before = row["body"] if row else None
        archive(cur, "documents", slug, "advisor")
        body = (before or "").rstrip() + "\n\n" + text.strip() if append and before \
            else text.strip()
        cur.execute(
            """INSERT INTO documents (slug, body, volatility, updated_at)
               VALUES (%s,%s,'high',now())
               ON CONFLICT (slug) DO UPDATE SET
                   body = excluded.body, updated_at = now()""",
            (slug, body))
        if before is None:
            summary = f"document « {slug} » créé"
        elif append:
            summary = f"document « {slug} » complété"
        else:
            summary = f"document « {slug} » réécrit ({len(before)} → {len(body)} car.)"
        _record(cur, "documents", slug, summary, before, body)
    store.commit()
    return summary


def write_memory(store: Store, text: str) -> str:
    """Record something durable the advisor worked out.

    For facts that outlive a conversation and should not have to be re-derived —
    that a pool is 25 m, that a course is hard, that a pain in one hip recurs
    under load. Not for opinions about a single session: that is a review.

    Kept apart from the profile because the profile is his words and this is the
    advisor's, and a month later nothing else would tell them apart.
    """
    text = text.strip()
    if not text:
        raise SystemExit("memory text is required")
    with store.cursor() as cur:
        cur.execute(
            "INSERT INTO advisor_memory (note, session_id) VALUES (%s,%s) RETURNING id",
            (text, os.environ.get(_SESSION_VAR)))
        mem_id = cur.fetchone()["id"]
        summary = f"mémoire #{mem_id} enregistrée"
        _record(cur, "advisor_memory", mem_id, summary, None, {"note": text})
    store.commit()
    return summary


def write_question(store: Store, key: str, text: str,
                   workout_id: int | None) -> str:
    """Record a question for the athlete that the record cannot answer.

    For the gap a review keeps running into — whether a hip settled, whether a
    session was the one the plan meant, what the wetsuit felt like. Previously
    these were asked inside the review prose, which is a place with no reply
    box: a session is reviewed once, so the question sat in a document that
    would never be rewritten, and the only way it reached anyone was the next
    review repeating it.

    `key` is chosen by the caller and is the identity of the question, so
    asking again is an update. Re-asking an *open* one bumps its count and the
    date — the athlete sees "asked three times", which is information about how
    much it matters. Re-asking an *answered* one changes nothing and reports
    the answer, because the answer is already in `ah-query context` and asking
    again means it was not read; a genuinely new question deserves a new key.
    """
    key, text = key.strip(), text.strip()
    if not key or not text:
        raise SystemExit("question key and text are required")
    with store.cursor() as cur:
        cur.execute("SELECT question, answer, answered_at, times_asked"
                    " FROM review_questions WHERE key = %s", (key,))
        row = cur.fetchone()
        if row and row["answer"]:
            return (f"« {key} » a déjà été répondue le "
                    f"{row['answered_at']:%Y-%m-%d} : {row['answer']}")
        if row:
            cur.execute(
                """UPDATE review_questions
                      SET question = %s, last_asked_at = now(),
                          times_asked = times_asked + 1
                    WHERE key = %s RETURNING times_asked""", (text, key))
            n = cur.fetchone()["times_asked"]
            summary = f"question « {key} » reposée ({n}× en tout)"
            _record(cur, "review_questions", key, summary,
                    {"question": row["question"], "times_asked": row["times_asked"]},
                    {"question": text, "times_asked": n})
        else:
            cur.execute(
                """INSERT INTO review_questions (key, workout_id, question)
                   VALUES (%s,%s,%s)""", (key, workout_id, text))
            summary = f"question « {key} » posée"
            _record(cur, "review_questions", key, summary, None,
                    {"question": text, "workout_id": workout_id})
    store.commit()
    return summary


def forget_memory(store: Store, mem_id: int) -> str:
    """Archive a remembered fact that turned out to be wrong or stale."""
    with store.cursor() as cur:
        cur.execute(
            """UPDATE advisor_memory SET archived_at = now()
                WHERE id = %s AND archived_at IS NULL RETURNING note""", (mem_id,))
        row = cur.fetchone()
        if row is None:
            raise SystemExit(f"no active memory {mem_id}")
        summary = f"mémoire #{mem_id} archivée"
        _record(cur, "advisor_memory", mem_id, summary, {"note": row["note"]}, None)
    store.commit()
    return summary


def recent(store: Store, limit: int) -> dict:
    """What has been changed, most recent first."""
    with store.cursor() as cur:
        cur.execute(
            """SELECT id, written_at, target, target_key, summary, undone_at
                 FROM advisor_writes ORDER BY written_at DESC LIMIT %s""", (limit,))
        return {"changes": [
            {"id": r["id"], "at": r["written_at"].isoformat(),
             "target": r["target"], "key": r["target_key"],
             "summary": r["summary"], "undone": r["undone_at"] is not None}
            for r in cur.fetchall()]}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Write notes, goals and documents. Never sensor data.")
    sub = ap.add_subparsers(dest="command", required=True)

    n = sub.add_parser("note", help="set the athlete's note on a session")
    n.add_argument("--id", type=int, required=True)
    n.add_argument("--text", required=True)

    g = sub.add_parser("goal", help="record a new goal")
    g.add_argument("--text", required=True)
    g.add_argument("--target-date", type=date.fromisoformat, default=None)
    g.add_argument("--id", type=int, default=None,
                   help="amend this goal instead of adding one")

    d = sub.add_parser("doc", help="write or extend a document")
    d.add_argument("--slug", required=True)
    d.add_argument("--text", required=True)
    d.add_argument("--append", action="store_true",
                   help="add to the end rather than replacing the whole document")

    m = sub.add_parser("memory", help="record something durable you worked out")
    m.add_argument("--text", required=True)

    q = sub.add_parser("question", help="ask the athlete something the record cannot answer")
    q.add_argument("--key", required=True,
                   help="stable identity; re-using it re-asks rather than duplicating")
    q.add_argument("--text", required=True)
    q.add_argument("--id", type=int, default=None,
                   help="the session that prompted it, if there is one")

    f = sub.add_parser("forget", help="archive a remembered fact")
    f.add_argument("--id", type=int, required=True)

    c = sub.add_parser("changes", help="what has been changed recently")
    c.add_argument("--limit", type=int, default=10)

    args = ap.parse_args(argv)
    store = Store(None)
    try:
        if args.command == "note":
            print(write_note(store, args.id, args.text))
        elif args.command == "goal":
            print(write_goal(store, args.text, args.target_date, args.id))
        elif args.command == "question":
            print(write_question(store, args.key, args.text, args.id))
        elif args.command == "memory":
            print(write_memory(store, args.text))
        elif args.command == "forget":
            print(forget_memory(store, args.id))
        elif args.command == "doc":
            print(write_doc(store, args.slug, args.text, args.append))
        else:
            json.dump(recent(store, args.limit), sys.stdout, indent=1,
                      ensure_ascii=False)
            print()
    finally:
        store.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
