"""Spitch — Nigerian-accented TTS (read-aloud) and STT (dictation).

Read-aloud must NOT use the browser voice; it uses Spitch so the accent is
Nigerian. Long notes are stripped of HTML and chunked (Spitch has per-request
length limits), then the MP3 segments are concatenated for playback.
"""

from __future__ import annotations

import logging
import re

from app.config import settings

logger = logging.getLogger("studymaster.spitch")

try:
    from spitch import AsyncSpitch

    _client = AsyncSpitch(api_key=settings.spitch_api_key) if settings.spitch_api_key else None
except Exception as exc:  # noqa: BLE001
    logger.error("Spitch SDK unavailable: %s", exc)
    _client = None

MAX_CHARS = 900  # per TTS request


def available() -> bool:
    return _client is not None


def html_to_speech_text(html: str) -> str:
    """Strip HTML to readable prose; keep figure captions (as image descriptions)."""
    text = re.sub(r"<figcaption[^>]*>(.*?)</figcaption>", r". Image: \1. ", html or "", flags=re.I | re.S)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"&nbsp;", " ", text)
    text = re.sub(r"&amp;", "and", text)
    text = re.sub(r"\$([^$]*)\$", r" \1 ", text)  # drop $ math delimiters for speech
    text = re.sub(r"\s+", " ", text).strip()
    return text


def _chunk(text: str, size: int = MAX_CHARS) -> list[str]:
    chunks, cur = [], ""
    for sentence in re.split(r"(?<=[.!?])\s+", text):
        if len(cur) + len(sentence) + 1 > size:
            if cur:
                chunks.append(cur.strip())
            cur = sentence
        else:
            cur = f"{cur} {sentence}".strip()
    if cur:
        chunks.append(cur.strip())
    return chunks or [text[:size]]


async def text_to_speech(text: str, *, voice: str | None = None, language: str | None = None) -> bytes:
    """Return MP3 bytes for ``text`` (concatenated across chunks). Empty on failure."""
    if not _client:
        return b""
    voice = voice or settings.spitch_voice
    language = language or settings.spitch_language
    audio = bytearray()
    for chunk in _chunk(text):
        try:
            resp = await _client.speech.generate(
                text=chunk, voice=voice, language=language, format="mp3"
            )
            audio.extend(await resp.read())
        except Exception as exc:  # noqa: BLE001
            logger.error("Spitch TTS failed: %s", exc)
    return bytes(audio)


async def transcribe(content: bytes, *, language: str | None = None) -> str:
    """Transcribe spoken audio to text (for the microphone/dictation feature)."""
    if not _client:
        return ""
    language = language or settings.spitch_language
    try:
        result = await _client.speech.transcribe(content=content, language=language)
        return getattr(result, "text", "") or ""
    except Exception as exc:  # noqa: BLE001
        logger.error("Spitch STT failed: %s", exc)
        return ""
