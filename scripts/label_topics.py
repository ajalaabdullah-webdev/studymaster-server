"""Backfill the ``topic`` field for the whole question bank.

Why: 94% of the 23k questions shipped with topic="Unknown" (only mathematics was
fully labelled), so topic search worked for maths and failed everywhere else —
e.g. "basic grammar" in English matched nothing, because grammar questions never
contain the word "grammar".

This asks a cheap model to name the topic of each question, in batches, and
writes ``topic`` / ``subtopic`` / ``topicSource="ai"``.

Safe to re-run: it only touches questions that are still unlabelled, so an
interrupted run simply resumes. Existing scraped labels are never overwritten.

    python scripts/label_topics.py                 # whole bank
    python scripts/label_topics.py --subject economics --limit 200
    python scripts/label_topics.py --dry-run       # show labels, write nothing
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pymongo import UpdateOne

from app.core.llm import chat_json
from app.db import init_db
from app.models.question import Question

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("label_topics")
logging.getLogger("LiteLLM").setLevel(logging.WARNING)

UNLABELLED = [None, "", "Unknown", "unknown", "N/A", "General"]

BATCH_SIZE = 25       # questions per LLM call
CONCURRENCY = 6       # parallel calls
MAX_CHARS = 420       # per question sent to the model


def _clip(text: str, n: int = MAX_CHARS) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= n else text[: n - 1] + "…"


def _prompt(subject: str, items: list[dict[str, Any]]) -> str:
    lines = []
    for i, q in enumerate(items):
        body = _clip(q.get("question_text", ""))
        opts = q.get("options")
        if isinstance(opts, dict) and opts:
            body += "  OPTIONS: " + "; ".join(f"{k}) {v}" for k, v in list(opts.items())[:4])
        elif isinstance(opts, list) and opts:
            body += "  OPTIONS: " + "; ".join(str(o) for o in opts[:4])
        lines.append(f"{i}. {_clip(body, 500)}")
    listing = "\n".join(lines)
    return f"""You are a Nigerian WAEC/JAMB examiner classifying past questions in {subject.replace('-', ' ').upper()}.

For EACH numbered question, give the syllabus topic it tests.

RULES:
- "topic" = a standard syllabus topic in Title Case, 1-4 words (e.g. "Number Bases",
  "Photosynthesis", "Concord", "Demand And Supply", "Balance Sheet").
- "subtopic" = optional finer label, 1-4 words, or "" if none.
- Use the SAME wording for the same topic across questions — consistency matters
  more than creativity. Prefer well-known syllabus names over invented ones.
- Classify what the question TESTS, not what it mentions in passing. A sentence
  that must be completed with the right verb form is "Concord" or "Tenses", not
  the subject of the sentence.
- Literature questions about a specific novel/play: topic = the work's theme area
  (e.g. "Character Analysis", "Plot", "Setting"), not the book title.
- If a question is truly unclassifiable, use "General".

QUESTIONS:
{listing}

Return STRICT JSON only:
{{"labels": [{{"i": 0, "topic": "...", "subtopic": "..."}}, ...]}}
Include one entry for EVERY question index 0-{len(items) - 1}."""


def _clean_label(v: Any, max_words: int = 5) -> str:
    if not isinstance(v, str):
        return ""
    s = " ".join(v.split()).strip(" .,:;-")
    if not s or len(s) > 60:
        return ""
    words = s.split()
    if len(words) > max_words:
        return ""
    return " ".join(w if w.isupper() and len(w) <= 4 else w.capitalize() for w in words)


async def _label_batch(subject: str, items: list[dict[str, Any]]) -> list[UpdateOne]:
    try:
        data = await chat_json(
            [{"role": "user", "content": _prompt(subject, items)}],
            tier="cheap", temperature=0.0,
        )
    except Exception as exc:  # noqa: BLE001 — skip this batch, keep going
        logger.warning("batch failed (%s): %s", subject, exc)
        return []

    labels = data.get("labels") if isinstance(data, dict) else None
    if not isinstance(labels, list):
        return []

    ops: list[UpdateOne] = []
    for entry in labels:
        if not isinstance(entry, dict):
            continue
        try:
            idx = int(entry.get("i", -1))
        except (TypeError, ValueError):
            continue
        if not 0 <= idx < len(items):
            continue
        topic = _clean_label(entry.get("topic"))
        if not topic:
            continue
        update: dict[str, Any] = {"topic": topic, "topicSource": "ai"}
        sub = _clean_label(entry.get("subtopic"))
        if sub and sub.lower() != topic.lower():
            update["subtopic"] = sub
        ops.append(UpdateOne({"_id": items[idx]["_id"]}, {"$set": update}))
    return ops


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--subject", help="only this subject slug")
    ap.add_argument("--limit", type=int, help="max questions to label this run")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    await init_db()
    col = Question.get_pymongo_collection()

    query: dict[str, Any] = {"topic": {"$in": UNLABELLED}}
    if args.subject:
        query["subject"] = args.subject.lower()

    remaining = await col.count_documents(query)
    todo = min(remaining, args.limit) if args.limit else remaining
    logger.info("unlabelled: %s | labelling this run: %s", remaining, todo)
    if not todo:
        logger.info("nothing to do — bank is fully labelled")
        return

    cursor = col.find(query, {"question_text": 1, "options": 1, "subject": 1}).limit(todo)
    docs = await cursor.to_list(length=todo)

    # Group by subject so each call sees one syllabus at a time.
    by_subject: dict[str, list[dict[str, Any]]] = {}
    for d in docs:
        by_subject.setdefault(d.get("subject", "unknown"), []).append(d)

    batches: list[tuple[str, list[dict[str, Any]]]] = []
    for subject, items in by_subject.items():
        for i in range(0, len(items), BATCH_SIZE):
            batches.append((subject, items[i : i + BATCH_SIZE]))
    logger.info("subjects: %s | batches: %s", len(by_subject), len(batches))

    sem = asyncio.Semaphore(CONCURRENCY)
    done = {"batches": 0, "labelled": 0}

    async def run(subject: str, items: list[dict[str, Any]]) -> None:
        async with sem:
            ops = await _label_batch(subject, items)
            if ops and not args.dry_run:
                await col.bulk_write(ops, ordered=False)
            if ops and args.dry_run:
                for op in ops[:3]:
                    logger.info("  would set %s", op._doc["$set"])
            done["batches"] += 1
            done["labelled"] += len(ops)
            if done["batches"] % 10 == 0 or done["batches"] == len(batches):
                logger.info(
                    "progress %s/%s batches | %s labelled",
                    done["batches"], len(batches), done["labelled"],
                )

    await asyncio.gather(*(run(s, items) for s, items in batches))

    still = await col.count_documents(query)
    logger.info("DONE — labelled %s; unlabelled remaining: %s", done["labelled"], still)


if __name__ == "__main__":
    asyncio.run(main())
