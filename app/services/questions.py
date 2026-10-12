"""Question-bank helpers shared across features.

Central place for querying the ~23k past questions, so quality-filtering and the
WAEC/JAMB-only rule live in one spot.
"""

from __future__ import annotations

from typing import Any

from app.core.subjects import normalize_many
from app.models.question import Question

# The platform only surfaces WAEC + JAMB (NECO is dropped).
ACTIVE_EXAMS = ["WAEC", "JAMB"]

# Questions students are allowed to see: not marked broken/needs_passage.
USABLE_QUALITY = {"pending", "ok", "fixed"}


def _exam_filter(exams: list[str] | None) -> list[str]:
    if not exams:
        return list(ACTIVE_EXAMS)
    return [e.upper() for e in exams if e.upper() in ACTIVE_EXAMS]


async def available_subjects_for_exams(exams: list[str] | None = None) -> set[str]:
    """Distinct subjects (lowercased) that have questions for the given exams."""
    exam_list = _exam_filter(exams)
    subjects = await Question.distinct("subject", {"exam_name": {"$in": exam_list}})
    return {str(s).lower().strip() for s in subjects if s}


async def pick_diagnostic_questions(
    subjects: list[str],
    exams: list[str],
    count: int = 10,
) -> list[Question]:
    """Sample a spread of objective questions across the student's subjects.

    - objective only (auto-markable for a fast baseline),
    - excludes questions flagged broken/needs_passage,
    - distributes ``count`` roughly evenly across subjects,
    - random sample so retakes differ.
    """
    exam_list = _exam_filter(exams)
    # Normalise any human/LLM subject names to the canonical DB slugs.
    subjects = normalize_many(subjects)
    if not subjects:
        subjects = sorted(await available_subjects_for_exams(exam_list))[:5]
    if not subjects:
        return []

    per_subject = max(1, count // len(subjects))
    picked: list[Question] = []
    seen_ids: set[Any] = set()

    for subject in subjects:
        remaining = count - len(picked)
        if remaining <= 0:
            break
        take = min(per_subject, remaining) if subject != subjects[-1] else remaining
        match: dict[str, Any] = {
            "subject": subject,
            "exam_name": {"$in": exam_list},
            "question_type": "obj",
            "correct_answer": {"$nin": [None, "", "Not available"]},
            "options": {"$ne": None},
            "$or": [
                {"qualityStatus": {"$exists": False}},
                {"qualityStatus": {"$in": list(USABLE_QUALITY)}},
            ],
        }
        docs = await Question.aggregate(
            [{"$match": match}, {"$sample": {"size": take * 2}}]
        ).to_list()
        for d in docs:
            if len(picked) >= count:
                break
            if d["_id"] in seen_ids:
                continue
            seen_ids.add(d["_id"])
            picked.append(Question.model_validate(d))

    return picked[:count]
