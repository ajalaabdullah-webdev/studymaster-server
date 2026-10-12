"""Backfill embeddings so questions can be found by MEANING, not just wording.

Why this exists: a grammar question reads "Choose the option that best completes
the sentence. He did not attend the final burial..." — the word "grammar" never
appears, so no keyword search can find it.

Measured detail that shapes this script: embedding the raw question text alone is
NOT enough (cosine to the query "basic grammar" was 0.15). Embedding the question
together with its topic label roughly doubles it (0.31). So run
``scripts/label_topics.py`` FIRST, then this.

Safe to re-run: only embeds questions that have no vector yet, so an interrupted
run resumes. Use --refresh to re-embed everything (e.g. after re-labelling).

    python scripts/embed_questions.py
    python scripts/embed_questions.py --subject english-language --limit 500
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

from app.config import settings
from app.core.llm import embed
from app.db import init_db
from app.models.question import Question

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("embed_questions")
logging.getLogger("LiteLLM").setLevel(logging.WARNING)

BATCH_SIZE = 128      # inputs per embedding request
CONCURRENCY = 4
MAX_CHARS = 700


def build_text(doc: dict[str, Any]) -> str:
    """What we embed: subject + labels + question + options.

    The labels matter — they carry the vocabulary a student actually searches
    with ("grammar", "concord"), which the question text itself rarely contains.
    """
    parts = [(doc.get("subject") or "").replace("-", " ")]
    topic = doc.get("topic")
    if topic and topic != "Unknown":
        parts.append(f"Topic: {topic}")
    if doc.get("subtopic"):
        parts.append(f"Subtopic: {doc['subtopic']}")
    q = " ".join((doc.get("question_text") or "").split())
    parts.append(f"Question: {q}")
    opts = doc.get("options")
    if isinstance(opts, dict) and opts:
        parts.append("Options: " + "; ".join(str(v) for v in list(opts.values())[:4]))
    elif isinstance(opts, list) and opts:
        parts.append("Options: " + "; ".join(str(o) for o in opts[:4]))
    text = ". ".join(p for p in parts if p)
    return text[:MAX_CHARS]


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--subject")
    ap.add_argument("--limit", type=int)
    ap.add_argument("--refresh", action="store_true", help="re-embed even if a vector exists")
    args = ap.parse_args()

    await init_db()
    col = Question.get_pymongo_collection()

    query: dict[str, Any] = {} if args.refresh else {"embedding": {"$in": [None, []]}}
    if args.subject:
        query["subject"] = args.subject.lower()

    remaining = await col.count_documents(query)
    todo = min(remaining, args.limit) if args.limit else remaining
    logger.info(
        "model=%s dims=%s | to embed: %s of %s",
        settings.embedding_model, settings.embedding_dimensions, todo, remaining,
    )
    if not todo:
        logger.info("nothing to do")
        return

    cursor = col.find(
        query, {"question_text": 1, "options": 1, "subject": 1, "topic": 1, "subtopic": 1}
    ).limit(todo)
    docs = await cursor.to_list(length=todo)

    batches = [docs[i : i + BATCH_SIZE] for i in range(0, len(docs), BATCH_SIZE)]
    sem = asyncio.Semaphore(CONCURRENCY)
    done = {"n": 0, "vec": 0}

    async def run(batch: list[dict[str, Any]]) -> None:
        async with sem:
            texts = [build_text(d) for d in batch]
            try:
                vectors = await embed(texts)
            except Exception as exc:  # noqa: BLE001 — skip batch, keep going
                logger.warning("embed batch failed: %s", exc)
                return
            ops = [
                UpdateOne({"_id": d["_id"]}, {"$set": {"embedding": v}})
                for d, v in zip(batch, vectors)
                if v
            ]
            if ops:
                await col.bulk_write(ops, ordered=False)
            done["n"] += 1
            done["vec"] += len(ops)
            if done["n"] % 10 == 0 or done["n"] == len(batches):
                logger.info("progress %s/%s batches | %s vectors", done["n"], len(batches), done["vec"])

    await asyncio.gather(*(run(b) for b in batches))
    left = await col.count_documents({"embedding": {"$in": [None, []]}})
    logger.info("DONE — embedded %s; without vectors remaining: %s", done["vec"], left)


if __name__ == "__main__":
    asyncio.run(main())
