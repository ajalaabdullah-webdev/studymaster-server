"""Topic-based question search — hybrid of exact labels and meaning.

The problem this solves: 94% of the 23k questions shipped with topic="Unknown"
(only mathematics was fully labelled), and exam questions rarely contain the
words a student searches with. A real English question reads

    "Choose the option that best completes the sentence. He did not attend the
     final burial............"

which IS a basic-grammar question, but contains neither "basic" nor "grammar" —
so no amount of keyword matching can ever find it.

Three layers, cheapest first:

1. LABEL + TEXT match (zero tokens): the phrase against ``topic``/``subtopic``
   (now AI-backfilled for the whole bank by ``scripts/label_topics.py``) and the
   question text.
2. KEYWORD expansion: ONE tiny LLM call per (subject, topic), cached forever in
   Mongo, turning "number bases" into binary/octal/denary/…
3. SEMANTIC search: Atlas Vector Search over embeddings of question + label
   (``scripts/embed_questions.py``). This is what catches paraphrases and
   questions whose wording shares nothing with the query.

Measured while building this: embedding the raw question alone scored 0.15
against "basic grammar"; embedding it together with its topic label scored 0.31 —
which is why the vectors include the labels.

Results are merged and ranked so exact label matches always outrank fuzzy ones,
and the whole thing degrades gracefully: if embeddings or the vector index are
missing, it silently falls back to layers 1-2.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from typing import Any, Optional

from beanie import Document
from pydantic import Field

from app.config import settings
from app.core.llm import chat_json, embed
from app.models.question import Question

logger = logging.getLogger("studymaster.topic_search")

VECTOR_INDEX = "questions_vector"

# Relative weights — exact label beats text match beats semantic neighbour.
W_LABEL = 100.0
W_TEXT = 60.0
W_KEYWORD = 8.0
W_SEMANTIC = 40.0  # scaled by cosine similarity (0-1)

# Below this cosine, a "neighbour" is usually unrelated; don't show it.
MIN_SIMILARITY = 0.28


class TopicKeywords(Document):
    """Cached keyword expansion for a (subject, topic) pair."""

    key: str  # f"{subject}::{topic-normalised}"
    subject: str
    topic: str
    keywords: list[str] = Field(default_factory=list)
    createdAt: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    class Settings:
        name = "topic_keywords"

    model_config = {"extra": "allow"}


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").lower().strip())


def _rx(term: str) -> re.Pattern:
    return re.compile(re.escape(term), re.IGNORECASE)


async def _expand_keywords(subject: str, topic: str) -> list[str]:
    key = f"{subject}::{_norm(topic)}"
    cached = await TopicKeywords.find_one(TopicKeywords.key == key)
    if cached:
        return cached.keywords
    try:
        data = await chat_json(
            [{"role": "user", "content": f"""For the Nigerian WAEC/JAMB subject "{subject}", list 8-14 short keywords
and phrases that appear in past-exam questions on the topic "{topic}" — synonyms, sub-concepts, notation words.
Example: "number bases" -> ["base two", "binary", "octal", "denary", "base ten", "bicimal", "convert", "base"].
Return JSON: {{"keywords": ["...", ...]}}"""}],
            tier="default", temperature=0.2,
        )
        keywords = [
            _norm(k) for k in (data.get("keywords", []) if isinstance(data, dict) else [])
            if isinstance(k, str) and 2 <= len(k.strip()) <= 40
        ]
    except Exception as exc:  # noqa: BLE001 — search still works without expansion
        logger.error("keyword expansion failed for %r: %s", topic, exc)
        keywords = []
    keywords = list(dict.fromkeys([_norm(topic)] + keywords))[:15]
    await TopicKeywords(key=key, subject=subject, topic=topic, keywords=keywords).insert()
    return keywords


async def _semantic_search(
    *, query: str, base: dict[str, Any], limit: int
) -> list[tuple[float, dict[str, Any]]]:
    """Atlas Vector Search over question+label embeddings.

    Returns (similarity, raw doc) pairs. Any failure (no index yet, no vectors,
    older cluster) is logged and treated as "no semantic results" so the caller
    still gets keyword results.
    """
    try:
        vector = (await embed([query]))[0]
    except Exception as exc:  # noqa: BLE001
        logger.warning("embedding the query failed: %s", exc)
        return []

    # $vectorSearch pre-filters must use the indexed filter fields only.
    vfilter: dict[str, Any] = {
        k: v for k, v in base.items() if k in {"subject", "exam_name", "question_type"}
    }
    pipeline = [
        {
            "$vectorSearch": {
                "index": VECTOR_INDEX,
                "path": "embedding",
                "queryVector": vector,
                "numCandidates": max(200, limit * 20),
                "limit": limit * 3,
                "filter": vfilter,
            }
        },
        {"$addFields": {"score": {"$meta": "vectorSearchScore"}}},
        {"$project": {"embedding": 0}},
    ]
    try:
        cursor = await Question.get_pymongo_collection().aggregate(pipeline)
        docs = await cursor.to_list(length=limit * 3)
    except Exception as exc:  # noqa: BLE001 — index may not exist yet
        logger.info("vector search unavailable (%s) — using keyword layers only", str(exc)[:120])
        return []
    return [(float(d.get("score", 0.0)), d) for d in docs]


def _serialise(doc: dict[str, Any]) -> dict[str, Any]:
    doc = {k: v for k, v in doc.items() if k != "embedding"}
    doc["_id"] = str(doc.get("_id"))
    if isinstance(doc.get("createdAt"), datetime):
        doc["createdAt"] = doc["createdAt"].isoformat()
    return doc


async def search_by_topic(
    *,
    subject: str,
    topic: str,
    exam_name: Optional[str] = None,
    question_type: str = "obj",
    limit: int = 20,
) -> list[dict[str, Any]]:
    subject = _norm(subject)
    topic_n = _norm(topic)
    base: dict[str, Any] = {"subject": subject, "question_type": question_type}
    base["exam_name"] = exam_name.upper() if exam_name else {"$in": ["WAEC", "JAMB"]}

    scores: dict[Any, float] = {}
    docs: dict[Any, dict[str, Any]] = {}

    def add(doc: dict[str, Any], score: float) -> None:
        _id = doc.get("_id")
        if _id is None:
            return
        docs.setdefault(_id, doc)
        scores[_id] = max(scores.get(_id, 0.0), score)

    col = Question.get_pymongo_collection()
    proj = {"embedding": 0}

    # --- Layer 1: label + text match (free) ---
    rx_topic = {"$regex": re.escape(topic_n), "$options": "i"}
    cursor = await col.find(
        {**base, "$or": [{"topic": rx_topic}, {"subtopic": rx_topic}, {"question_text": rx_topic}]},
        proj,
    ).limit(limit * 4).to_list(length=limit * 4)
    for d in cursor:
        label_hit = _rx(topic_n).search(f"{d.get('topic') or ''} {d.get('subtopic') or ''}")
        add(d, W_LABEL if label_hit else W_TEXT)

    # --- Layer 3 runs even when layer 1 succeeded: it finds the questions whose
    # wording shares nothing with the query, which is the whole point. ---
    semantic = await _semantic_search(query=f"{subject.replace('-', ' ')} {topic}", base=base, limit=limit)
    for sim, d in semantic:
        if sim >= MIN_SIMILARITY:
            add(d, W_SEMANTIC * sim)

    # --- Layer 2: keyword expansion, only if we're still short ---
    if len([s for s in scores.values() if s > 0]) < limit:
        keywords = await _expand_keywords(subject, topic)
        extra = [k for k in keywords if k != topic_n]
        if extra:
            rx = "|".join(re.escape(k) for k in extra)
            cands = await col.find(
                {**base, "$or": [{"topic": {"$regex": rx, "$options": "i"}},
                                 {"question_text": {"$regex": rx, "$options": "i"}}]},
                proj,
            ).limit(600).to_list(length=600)
            pats = [_rx(k) for k in extra]
            for d in cands:
                text = f"{d.get('topic') or ''} {d.get('subtopic') or ''} {d.get('question_text') or ''}"
                hits = sum(1 for p in pats if p.search(text))
                if hits:
                    add(d, W_KEYWORD * hits)

    ranked = sorted(scores.items(), key=lambda kv: -kv[1])[:limit]
    return [_serialise(docs[_id]) for _id, _ in ranked]


async def list_topics(*, subject: str, exam_name: Optional[str] = None, limit: int = 60) -> list[dict[str, Any]]:
    """The topics that actually exist for a subject, most-covered first.

    Only possible now that the bank is labelled — it lets the UI OFFER topics
    instead of making students guess what to type.
    """
    # "General"/"Unknown" are catch-alls, not topics — never offer them as chips.
    match: dict[str, Any] = {
        "subject": _norm(subject),
        "topic": {"$nin": [None, "", "Unknown", "General", "Miscellaneous"]},
    }
    if exam_name:
        match["exam_name"] = exam_name.upper()
    pipeline = [
        {"$match": match},
        {"$group": {"_id": "$topic", "count": {"$sum": 1}}},
        {"$sort": {"count": -1}},
        {"$limit": limit},
    ]
    cursor = await Question.get_pymongo_collection().aggregate(pipeline)
    rows = await cursor.to_list(length=limit)
    return [{"topic": r["_id"], "count": r["count"]} for r in rows]
