"""Roadmap — the flexible, visual study plan generated after onboarding.

Structure (three levels, built for a Candy-Crush-style visual path):

    Roadmap
      └─ phases[]        big arcs, e.g. Foundations → Core → Exam Prep → Final Sprint
      └─ levels[]        the visual nodes on the path — ONE PER WEEK
           └─ tasks[]    concrete actions for that week (study / practice / exam / review)

The roadmap is *flexible*: it is regenerated/adjusted as the student's real activity
comes in (a teacher re-planning), so `version`, `generatedAt`, and `adjustedAt` track that.
Task completion is driven by the activity stream, not manual ticking alone.
"""

from datetime import datetime, timezone
from typing import Any, Literal, Optional

from beanie import Document, PydanticObjectId
from pydantic import BaseModel, Field

TaskType = Literal["study", "practice", "exam", "review", "milestone"]
NodeStatus = Literal["locked", "available", "in_progress", "done"]


class RoadmapTask(BaseModel):
    id: str
    type: TaskType
    subject: Optional[str] = None  # canonical slug
    topic: Optional[str] = None  # curriculum topic (LLM-derived; may not match DB topic)
    title: str
    description: str = ""
    targetCount: int = 0  # e.g. practise 20 questions
    estMinutes: int = 30
    status: NodeStatus = "available"
    completedAt: Optional[datetime] = None

    model_config = {"extra": "allow"}


class RoadmapLevel(BaseModel):
    order: int  # 1-based position on the path
    weekNumber: int
    phaseKey: str
    title: str
    goal: str = ""
    subjectsFocus: list[str] = Field(default_factory=list)
    startDate: Optional[datetime] = None
    endDate: Optional[datetime] = None
    status: NodeStatus = "locked"
    tasks: list[RoadmapTask] = Field(default_factory=list)

    model_config = {"extra": "allow"}


class RoadmapPhase(BaseModel):
    key: str
    title: str
    order: int
    goal: str = ""
    weeks: list[int] = Field(default_factory=list)

    model_config = {"extra": "allow"}


class Roadmap(Document):
    userId: PydanticObjectId
    exams: list[str] = Field(default_factory=list)
    subjects: list[str] = Field(default_factory=list)  # canonical slugs
    career: Optional[str] = None
    targetScores: dict[str, Any] = Field(default_factory=dict)

    startDate: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    examDate: Optional[datetime] = None  # target finish (nearest relevant exam)
    horizonWeeks: int = 0
    examCalendar: dict[str, Any] = Field(default_factory=dict)  # {"JAMB": {...}, "WAEC": {...}}

    phases: list[RoadmapPhase] = Field(default_factory=list)
    levels: list[RoadmapLevel] = Field(default_factory=list)

    summary: str = ""
    status: Literal["active", "archived"] = "active"
    version: int = 1
    generatedAt: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    adjustedAt: Optional[datetime] = None

    class Settings:
        name = "roadmaps"

    model_config = {"populate_by_name": True, "extra": "allow"}
