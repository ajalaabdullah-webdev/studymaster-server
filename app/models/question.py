"""Question document — maps to the existing ``questions`` collection (~20k
scraped WAEC/NECO/JAMB past questions).

Legacy fields are mirrored exactly. V2 adds:
- passage linkage (``passageGroupId`` / ``passage``) to fix comprehension
  questions whose passage lives in a different record;
- ``qualityStatus`` produced by the offline cleanup agent so incomplete
  questions can be filtered out of what students see.

NECO records still exist in the data; the application layer filters to
WAEC + JAMB, so we keep NECO in the enum to load legacy docs without errors.
"""

from datetime import datetime, timezone
from typing import Any, Literal, Optional

from beanie import Document
from pydantic import Field


class Question(Document):
    subject: str  # stored lowercase
    year: Optional[int] = None
    question_number: Optional[str] = None
    # Data contains three types: obj (21k), theory (1.7k), practical (160).
    question_type: Literal["theory", "obj", "practical"]
    exam_name: Literal["WAEC", "NECO", "JAMB"]
    exam_year: Optional[str] = None
    question_text: str
    # Objective options: legacy data is untyped (array OR object OR null).
    options: Any = None
    correct_answer: str = "Not available"
    explanation: str = "Not available"
    images: list[str] = Field(default_factory=list)
    topic: str = "Unknown"
    source_url: str = ""
    createdAt: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    # --- V2 additions ---
    # Groups a comprehension/cloze passage with the questions that depend on it.
    passageGroupId: Optional[str] = None
    # The passage text itself (populated on the anchor record or reconstructed).
    passage: Optional[str] = None
    # Offline cleanup verdict: has this question been checked and is it usable?
    #   pending  = not yet reviewed
    #   ok       = complete & self-contained
    #   fixed    = repaired (e.g. passage attached, answer/explanation filled)
    #   needs_passage = depends on a passage we could not link
    #   broken   = unusable, hide from students
    qualityStatus: Literal["pending", "ok", "fixed", "needs_passage", "broken"] = "pending"
    qualityNotes: Optional[str] = None

    # --- Topic labelling + semantic search ---
    # Where ``topic`` came from: "scrape" (original data) or "ai" (backfill).
    # 94% of the bank shipped with topic="Unknown", which made topic search fail
    # outside mathematics — the AI backfill fills those in.
    topicSource: Optional[Literal["scrape", "ai"]] = None
    # Finer-grained label under ``topic`` (e.g. topic="Tenses",
    # subtopic="Reported Speech"). Optional; used to sharpen search + practice.
    subtopic: Optional[str] = None
    # 256-dim embedding of question+options, for meaning-based search (Atlas
    # Vector Search). Excluded from normal reads via projections.
    embedding: Optional[list[float]] = None

    class Settings:
        name = "questions"
        # Existing compound index is managed by the legacy schema. New V2 indexes
        # (e.g. on qualityStatus) are created explicitly via a migration script to
        # avoid Beanie re-declaring the pre-existing ones.

    model_config = {"populate_by_name": True, "extra": "allow"}
