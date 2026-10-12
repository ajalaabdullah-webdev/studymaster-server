"""Persistent state for the standalone Live Learning classroom."""

from datetime import UTC, datetime
from typing import Any, ClassVar, Literal

from beanie import Document, PydanticObjectId
from pydantic import BaseModel, Field


class BoardOperation(BaseModel):
    id: str
    revision: int
    action: Literal[
        "set_focus", "write_heading", "write_text", "write_formula",
        "write_steps", "show_example", "show_question", "highlight_item",
        "clear_board", "new_page", "show_image",
    ]
    content: dict[str, Any] = Field(default_factory=dict)
    # Which roadmap step this line belongs to. The board shows one step at a
    # time — a real whiteboard is wiped between topics — so lines are paged by
    # this rather than accumulating into an unreadable wall.
    stepIndex: int = 0
    # "key" lines are the ones worth remembering and are highlighted.
    emphasis: Literal["normal", "key"] = "normal"
    createdAt: datetime = Field(default_factory=lambda: datetime.now(UTC))


class RoadmapStep(BaseModel):
    """One agreed stage of the lesson. The student sees these as progress."""

    title: str
    detail: str = ""
    status: Literal["pending", "active", "done"] = "pending"


class NoteLine(BaseModel):
    """The permanent record. Everything worth keeping lands here even after the
    board is wiped, so the student leaves with full notes."""

    text: str
    kind: Literal["point", "key", "formula", "working", "heading"] = "point"
    latex: str = ""
    stepIndex: int = 0
    createdAt: datetime = Field(default_factory=lambda: datetime.now(UTC))


class LessonTranscript(BaseModel):
    role: Literal["user", "assistant"]
    text: str
    createdAt: datetime = Field(default_factory=lambda: datetime.now(UTC))


class LiveLesson(Document):
    userId: PydanticObjectId
    subject: str
    topic: str
    goal: str = "Understand the topic"
    details: str = ""
    startingLevel: str = "new"
    targetMinutes: int = 30
    elapsedSeconds: int = 0
    status: Literal["preparing", "ready", "active", "paused", "completed", "failed"] = "preparing"
    openaiSessionId: str | None = None
    boardOperations: list[BoardOperation] = Field(default_factory=list)
    roadmap: list[RoadmapStep] = Field(default_factory=list)
    # Prepared board content per roadmap step (see services/lesson_pack.py):
    # [{title, detail, cards:[{id, kind, title, preview, ops:[...]}]}]
    pack: list[dict[str, Any]] = Field(default_factory=list)
    # The full lesson notes written before the class (services/notebook.py):
    # {"objectives": [...], "pages": [{"title", "ops": [board operations with ids]}], "hasQuiz"}
    notebook: dict[str, Any] = Field(default_factory=dict)
    quizResult: dict[str, Any] = Field(default_factory=dict)
    currentStepIndex: int = 0
    notes: list[NoteLine] = Field(default_factory=list)
    transcript: list[LessonTranscript] = Field(default_factory=list)
    summary: str = ""
    extensions: list[int] = Field(default_factory=list)
    createdAt: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updatedAt: datetime | None = None
    completedAt: datetime | None = None

    class Settings:
        name = "live_lessons"

    model_config: ClassVar[dict] = {"extra": "allow"}
