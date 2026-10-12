"""Exam document — maps the existing ``exams`` collection (timed practice exams)."""

from datetime import datetime, timezone
from typing import Any, Literal, Optional

from beanie import Document, PydanticObjectId
from pydantic import BaseModel, Field


class ExamQuestion(BaseModel):
    questionId: Optional[PydanticObjectId] = None
    userAnswer: Optional[str] = None
    isCorrect: bool = False
    aiFeedback: Any = None

    model_config = {"extra": "allow"}


class Exam(Document):
    userId: PydanticObjectId
    subject: str
    exam_name: Literal["JAMB", "NECO", "WAEC"]
    question_type: Literal["obj", "theory", "practical"] = "obj"
    questions: list[ExamQuestion] = Field(default_factory=list)
    score: int = 0
    totalQuestions: int = 60
    status: Literal["ongoing", "completed"] = "ongoing"
    completedAt: Optional[datetime] = None
    createdAt: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    class Settings:
        name = "exams"

    model_config = {"populate_by_name": True, "extra": "allow"}
