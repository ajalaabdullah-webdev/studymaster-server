"""Platform-wide settings (singleton document) — admin-controlled feature flags.

Currently: the brain-games registry. Admin can toggle the whole Gaming tab or
individual games; students only ever see enabled games.
"""

from datetime import datetime, timezone
from typing import Optional

from beanie import Document
from pydantic import BaseModel, Field

# Games shipped with the platform (new Friday releases get added here).
DEFAULT_GAMES = [
    {
        "key": "shape-challenge",
        "name": "Shape Challenge",
        "description": "Spot and match the shapes as fast as you can — top the leaderboard!",
        "enabled": True,
    },
]


class GameFlag(BaseModel):
    key: str
    name: str
    description: str = ""
    enabled: bool = True

    model_config = {"extra": "allow"}


class PlatformSettings(Document):
    key: str = "platform"  # singleton marker
    gamingEnabled: bool = True
    games: list[GameFlag] = Field(default_factory=lambda: [GameFlag(**g) for g in DEFAULT_GAMES])
    updatedAt: Optional[datetime] = None

    class Settings:
        name = "platform_settings"

    model_config = {"populate_by_name": True, "extra": "allow"}


async def get_platform_settings() -> "PlatformSettings":
    settings = await PlatformSettings.find_one(PlatformSettings.key == "platform")
    if not settings:
        settings = PlatformSettings()
        await settings.insert()
    # merge in any newly-registered default games
    known = {g.key for g in settings.games}
    changed = False
    for g in DEFAULT_GAMES:
        if g["key"] not in known:
            settings.games.append(GameFlag(**g))
            changed = True
    if changed:
        settings.updatedAt = datetime.now(timezone.utc)
        await settings.save()
    return settings
