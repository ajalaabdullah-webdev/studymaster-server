"""Quiz document — maps the existing ``quizzes`` collection (note quizzes)."""

from datetime import datetime, timezone
from typing import Literal, Optional

from beanie import Document, PydanticObjectId
from pydantic import BaseModel, Field


class QuizQuestion(BaseModel):
    question: str
    options: list[str] = Field(default_factory=list)
    correctAnswer: str = ""
    explanation: str = ""
    images: list[str] = Field(default_factory=list)

    model_config = {"extra": "allow"}


class QuizAttempt(BaseModel):
    score: int
    total: int
    at: datetime

    model_config = {"extra": "allow"}


class Quiz(Document):
    userId: PydanticObjectId
    noteTitle: str = ""
    noteId: Optional[PydanticObjectId] = None
    questions: list[QuizQuestion] = Field(default_factory=list)
    isPublic: bool = True
    score: int = 0
    status: Literal["ongoing", "completed"] = "ongoing"
    # Every submission is kept — progress is never reset to scratch.
    attempts: list[QuizAttempt] = Field(default_factory=list)
    createdAt: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    class Settings:
        name = "quizzes"

    model_config = {"populate_by_name": True, "extra": "allow"}
