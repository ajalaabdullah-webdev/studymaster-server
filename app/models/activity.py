"""Activity document — maps to the existing ``activities`` collection.

This is the behavioural signal stream the V2 readiness-probability engine
consumes (what the student does, when, and for how long).
"""

from datetime import datetime, timezone
from typing import Any, Literal, Optional

from beanie import Document, PydanticObjectId
from pydantic import Field

ActivityType = Literal[
    "note_creation",
    "note_study",
    "quiz_study",
    "past_question_study",
    "ai_chat",
    "app_session",
    # V2 additions:
    "diagnostic",
    "roadmap_task",
    "live_teach",
    "exam",
]


class Activity(Document):
    userId: PydanticObjectId
    type: ActivityType
    startTime: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    endTime: Optional[datetime] = None
    duration: int = 0  # seconds
    metadata: dict[str, Any] = Field(default_factory=dict)
    status: Literal["active", "completed"] = "active"
    # Mongoose `timestamps: true` writes these; declared so they are queryable.
    createdAt: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updatedAt: Optional[datetime] = None

    class Settings:
        name = "activities"

    model_config = {"populate_by_name": True, "extra": "allow"}
