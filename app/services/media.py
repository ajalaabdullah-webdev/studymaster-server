"""Cloudinary uploads (audio for read-aloud caching, images for community/uploads)."""

from __future__ import annotations

import io
import logging

import cloudinary
import cloudinary.uploader

from app.config import settings

logger = logging.getLogger("studymaster.media")

_configured = False


def _ensure() -> bool:
    global _configured
    if not settings.cloudinary_cloud_name:
        return False
    if not _configured:
        cloudinary.config(
            cloud_name=settings.cloudinary_cloud_name,
            api_key=settings.cloudinary_api_key,
            api_secret=settings.cloudinary_api_secret,
            secure=True,
        )
        _configured = True
    return True


def upload_image(data: bytes, *, folder: str = "studymaster/community") -> str | None:
    """Upload image bytes; return the secure URL (or None on failure)."""
    if not _ensure() or not data:
        return None
    try:
        res = cloudinary.uploader.upload(io.BytesIO(data), resource_type="image", folder=folder)
        return res.get("secure_url")
    except Exception as exc:  # noqa: BLE001
        logger.error("Cloudinary image upload failed: %s", exc)
        return None


def upload_audio(data: bytes, *, public_id: str | None = None) -> str | None:
    """Upload MP3 bytes; return the secure URL (or None on failure)."""
    if not _ensure() or not data:
        return None
    try:
        res = cloudinary.uploader.upload(
            io.BytesIO(data),
            resource_type="video",  # Cloudinary handles audio under "video"
            folder="studymaster/audio",
            public_id=public_id,
            format="mp3",
            overwrite=True,
        )
        return res.get("secure_url")
    except Exception as exc:  # noqa: BLE001
        logger.error("Cloudinary audio upload failed: %s", exc)
        return None
