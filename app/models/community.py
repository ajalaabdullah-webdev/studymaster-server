"""Community + gaming documents — map the existing ``posts``, ``comments``,
``followers`` and ``gameattempts`` collections."""

from datetime import datetime, timezone
from typing import Optional

from beanie import Document, PydanticObjectId
from pydantic import Field


def _now() -> datetime:
    return datetime.now(timezone.utc)


class Post(Document):
    userId: PydanticObjectId
    content: str
    likes: list[PydanticObjectId] = Field(default_factory=list)
    image: str = ""
    createdAt: datetime = Field(default_factory=_now)
    updatedAt: Optional[datetime] = None

    class Settings:
        name = "posts"

    model_config = {"populate_by_name": True, "extra": "allow"}


class Comment(Document):
    userId: PydanticObjectId
    postId: PydanticObjectId
    content: str
    createdAt: datetime = Field(default_factory=_now)
    updatedAt: Optional[datetime] = None

    class Settings:
        name = "comments"

    model_config = {"populate_by_name": True, "extra": "allow"}


class Follower(Document):
    followerId: PydanticObjectId
    followingId: PydanticObjectId
    createdAt: datetime = Field(default_factory=_now)

    class Settings:
        name = "followers"

    model_config = {"populate_by_name": True, "extra": "allow"}


class GameAttempt(Document):
    userId: PydanticObjectId
    timeTaken: int  # milliseconds
    correctCount: int = 0
    totalShapes: int = 10
    createdAt: datetime = Field(default_factory=_now)

    class Settings:
        name = "gameattempts"

    model_config = {"populate_by_name": True, "extra": "allow"}
