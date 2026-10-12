"""Speech endpoints — reusable across notes, coach, past questions, exams.

    POST /api/v1/speech/transcribe   multipart audio -> {text}   (dictation / mic)
    POST /api/v1/speech/tts          {text} -> audio/mpeg         (generic read-aloud)
"""

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from fastapi.responses import Response
from pydantic import BaseModel

from app.core.deps import get_current_user
from app.models.user import User
from app.services import openai_speech, spitch

router = APIRouter(prefix="/api/v1/speech", tags=["speech"])


class TTSRequest(BaseModel):
    text: str
    voice: str | None = None


@router.post("/transcribe")
async def transcribe(
    audio: UploadFile = File(...),
    current: User = Depends(get_current_user),
) -> dict:
    if not spitch.available():
        raise HTTPException(status_code=503, detail="Speech-to-text is not configured.")
    content = await audio.read()
    if not content:
        raise HTTPException(status_code=400, detail="Empty audio.")
    text = await spitch.transcribe(content)
    return {"success": True, "data": {"text": text}}


@router.post("/tts")
async def tts(body: TTSRequest, current: User = Depends(get_current_user)):
    if not openai_speech.available():
        raise HTTPException(status_code=503, detail="Text-to-speech is not configured.")
    text = (body.text or "").strip()
    if not text:
        raise HTTPException(status_code=400, detail="No text provided.")
    audio = await openai_speech.text_to_speech(text, voice=body.voice)
    if not audio:
        raise HTTPException(status_code=502, detail="Could not generate audio.")
    return Response(content=audio, media_type="audio/mpeg")
