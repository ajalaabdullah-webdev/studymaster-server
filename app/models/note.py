"""Note document — maps the existing ``notes`` collection.

Content is HTML (as in V1). V2 adds structured provenance: the images the agent
embedded (with descriptions, for read-aloud & live-teaching), the subject/topic,
a cached read-aloud audio URL, and the linked quiz.
"""

from datetime import datetime, timezone
from typing import Any, Optional

from beanie import Document, PydanticObjectId
from pydantic import BaseModel, Field


class NoteImage(BaseModel):
    url: str
    title: str = ""
    description: str = ""  # caption / alt — read aloud when reached
    source: str = "wikimedia"
    license: str = ""
    pageUrl: str = ""

    model_config = {"extra": "allow"}


class ChatTurn(BaseModel):
    role: str  # "user" | "assistant"
    message: str = ""
    versionNumber: Optional[int] = None

    model_config = {"extra": "allow"}


class NoteVersion(BaseModel):
    number: int
    content: str = ""
    images: list[NoteImage] = Field(default_factory=list)
    instruction: str = "Original note"
    basedOnVersion: Optional[int] = None
    quizId: Optional[PydanticObjectId] = None
    createdAt: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    model_config = {"extra": "allow"}


class Note(Document):
    userId: PydanticObjectId
    title: str
    content: str = ""  # HTML (with KaTeX-friendly maths and <figure> images)
    tag: str = "General"
    subject: Optional[str] = None  # canonical slug when known
    topic: Optional[str] = None

    images: list[NoteImage] = Field(default_factory=list)
    chatHistory: list[ChatTurn] = Field(default_factory=list)
    # Immutable content snapshots. Existing notes have an empty list and are
    # lazily backfilled as Version 1 the first time they are revised.
    versions: list[NoteVersion] = Field(default_factory=list)
    currentVersion: int = 1

    # Cached whole-note read-aloud audio (generated on demand, then reused).
    audioUrl: Optional[str] = None
    audioProvider: Optional[str] = None
    audioVersion: Optional[int] = None
    # Cached read-along data: {"segments": [{index, kind, spoken, audioUrl}]} for
    # the highlight-and-scroll player. Invalidated when content changes.
    readAlong: Optional[dict[str, Any]] = None

    isPublic: bool = True
    quizId: Optional[PydanticObjectId] = None

    createdAt: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updatedAt: Optional[datetime] = None

    class Settings:
        name = "notes"

    model_config = {"populate_by_name": True, "extra": "allow"}
