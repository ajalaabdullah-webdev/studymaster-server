"""OpenAI text-to-speech for note narration and generic read-aloud.

The note read-along remains segmented by ``note_speech``. Each generated clip
maps to one visible block, allowing the frontend to drive highlighting from the
real audio position instead of relying on estimated speech timing.
"""

from __future__ import annotations

import logging
import re

from openai import AsyncOpenAI

from app.config import settings

logger = logging.getLogger("studymaster.openai_speech")

_client = AsyncOpenAI(api_key=settings.openai_api_key) if settings.openai_api_key else None


def _chunks(text: str, limit: int = 4000) -> list[str]:
    """Keep requests below the Speech API's 4096-character input limit."""
    clean = re.sub(r"\s+", " ", text or "").strip()
    chunks: list[str] = []
    while clean:
        if len(clean) <= limit:
            chunks.append(clean)
            break
        cut = max(clean.rfind(mark, 0, limit) for mark in (". ", "? ", "! ", "; ", ", "))
        if cut < limit // 2:
            cut = clean.rfind(" ", 0, limit)
        if cut <= 0:
            cut = limit
        else:
            cut += 1
        chunks.append(clean[:cut].strip())
        clean = clean[cut:].strip()
    return chunks


def available() -> bool:
    return _client is not None


def provider_key() -> str:
    """Stable cache identity so clips from an older TTS provider are rebuilt."""
    return f"openai:{settings.openai_tts_model}:{settings.openai_tts_voice}"


async def text_to_speech(text: str, *, voice: str | None = None) -> bytes:
    """Return MP3 narration bytes, or an empty byte string on provider failure."""
    if not _client or not (text or "").strip():
        return b""
    audio = bytearray()
    for chunk in _chunks(text):
        try:
            response = await _client.audio.speech.create(
                model=settings.openai_tts_model,
                voice=voice or settings.openai_tts_voice,
                input=chunk,
                instructions=settings.openai_tts_instructions,
                response_format="mp3",
            )
            audio.extend(response.content)
        except Exception as exc:  # noqa: BLE001
            logger.error("OpenAI TTS failed: %s", exc)
            return b""
    return bytes(audio)
