"""DiagnosticSession — the onboarding baseline test (~10 questions across the
student's core subjects).

Its purpose is an *introduction*, not a judgement: it establishes where the
student is starting from so the roadmap and readiness probability have a
baseline. Questions are drawn from the vetted past-question pool (quality-checked)
and rendered with LaTeX where needed.
"""

from datetime import datetime, timezone
from typing import Any, Optional

from beanie import Document, PydanticObjectId
from pydantic import BaseModel, Field


class DiagnosticItem(BaseModel):
    questionId: Optional[PydanticObjectId] = None  # source past-question, if any
    subject: str
    exam_name: str
    question_text: str
    options: Any = None
    correct_answer: str
    userAnswer: Optional[str] = None
    isCorrect: Optional[bool] = None
    # True when this item was rendered from a synthesised/complete question
    # rather than a raw scraped one.
    synthesised: bool = False

    model_config = {"populate_by_name": True, "extra": "allow"}


class DiagnosticSession(Document):
    userId: PydanticObjectId
    exams: list[str] = Field(default_factory=list)
    career: Optional[str] = None
    subjects: list[str] = Field(default_factory=list)
    items: list[DiagnosticItem] = Field(default_factory=list)

    status: str = "in_progress"  # in_progress | completed
    score: int = 0
    total: int = 0
    # Per-subject baseline, e.g. {"mathematics": {"correct": 1, "total": 3}}.
    subjectBreakdown: dict[str, Any] = Field(default_factory=dict)

    createdAt: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    completedAt: Optional[datetime] = None

    class Settings:
        name = "diagnostic_sessions"

    model_config = {"populate_by_name": True, "extra": "allow"}
