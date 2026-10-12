"""Chat document — maps the existing ``chats`` collection (the Coach)."""

from datetime import datetime, timezone
from typing import Optional

from beanie import Document, PydanticObjectId
from pydantic import BaseModel, Field


def _now() -> datetime:
    return datetime.now(timezone.utc)


class ChatMessage(BaseModel):
    role: str  # "user" | "assistant"
    content: str
    timestamp: datetime = Field(default_factory=_now)

    model_config = {"extra": "allow"}


class Chat(Document):
    userId: PydanticObjectId
    title: str = "New Chat"
    questionId: Optional[PydanticObjectId] = None
    messages: list[ChatMessage] = Field(default_factory=list)
    createdAt: datetime = Field(default_factory=_now)
    updatedAt: datetime = Field(default_factory=_now)

    class Settings:
        name = "chats"

    model_config = {"populate_by_name": True, "extra": "allow"}
