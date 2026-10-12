"""Tidy topic labels so the same topic isn't split into several near-identical ones.

The bank mixes scraped labels ("Mathematics: Number Bases.") with AI labels
("Number Bases"), plus casing/plural drift. Search tolerates this, but the
browse-by-topic chips look broken when one topic appears three times.

Mechanical only — no LLM, nothing renamed to a different concept:
  • strip a leading "Subject: " prefix
  • strip trailing punctuation
  • collapse whitespace + normalise to Title Case
  • merge variants that differ only by case/punctuation/plural-s, keeping the
    most common spelling as the canonical one (per subject)

    python scripts/normalize_topics.py --dry-run
    python scripts/normalize_topics.py
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import re
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pymongo import UpdateMany

from app.db import init_db
from app.models.question import Question

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("normalize_topics")

SMALL = {"and", "of", "in", "to", "the", "for", "on", "at", "by", "with", "a", "an"}


def tidy(topic: str) -> str:
    s = " ".join((topic or "").split())
    s = re.sub(r"^[A-Za-z ]{3,25}:\s*", "", s)       # "Mathematics: X" -> "X"
    s = s.strip(" .,:;-")
    if not s:
        return ""
    words = s.split()
    out = []
    for i, w in enumerate(words):
        if w.isupper() and len(w) <= 4:               # keep acronyms (GDP, ICT)
            out.append(w)
        elif i and w.lower() in SMALL:
            out.append(w.lower())
        else:
            out.append(w[:1].upper() + w[1:].lower())
    return " ".join(out)


def merge_key(topic: str) -> str:
    """Variants that should collapse together: case, punctuation, trailing 's'."""
    k = re.sub(r"[^a-z0-9 ]", "", topic.lower())
    k = " ".join(w[:-1] if len(w) > 3 and w.endswith("s") else w for w in k.split())
    return k


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    await init_db()
    col = Question.get_pymongo_collection()

    cursor = await col.aggregate([
        {"$match": {"topic": {"$nin": [None, "", "Unknown"]}}},
        {"$group": {"_id": {"subject": "$subject", "topic": "$topic"}, "n": {"$sum": 1}}},
    ])
    rows = await cursor.to_list(length=100000)
    logger.info("distinct (subject, topic) pairs: %s", len(rows))

    # subject -> merge_key -> [(tidied, count, original)]
    groups: dict[str, dict[str, list[tuple[str, int, str]]]] = defaultdict(lambda: defaultdict(list))
    for r in rows:
        subject = r["_id"]["subject"]
        original = r["_id"]["topic"]
        t = tidy(original)
        if not t:
            continue
        groups[subject][merge_key(t)].append((t, r["n"], original))

    ops: list[Any] = []
    merged = 0
    for subject, keys in groups.items():
        for _key, variants in keys.items():
            # canonical = most common tidied spelling
            best = max(variants, key=lambda v: (v[1], -len(v[0])))[0]
            for tidied, _n, original in variants:
                if original != best:
                    ops.append(UpdateMany(
                        {"subject": subject, "topic": original}, {"$set": {"topic": best}}
                    ))
                    if tidied != best:
                        merged += 1

    logger.info("updates to apply: %s (variant merges: %s)", len(ops), merged)
    for op in ops[:8]:
        logger.info("   %r -> %r", op._filter["topic"], op._doc["$set"]["topic"])

    if args.dry_run:
        logger.info("dry run — nothing written")
        return
    for i in range(0, len(ops), 500):
        await col.bulk_write(ops[i : i + 500], ordered=False)
    after = len(await col.distinct("topic"))
    logger.info("DONE — distinct topics now: %s", after)


if __name__ == "__main__":
    asyncio.run(main())
