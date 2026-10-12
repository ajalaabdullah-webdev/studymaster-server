"""Notes endpoints — agentic generation (SSE), library, read-aloud, quiz.

    POST /api/v1/notes/generate       SSE stream: progress events then the note
    POST /api/v1/notes/{id}/edit      SSE stream: apply a chat instruction
    GET  /api/v1/notes                library list
    GET  /api/v1/notes/{id}           one note (+ its quiz)
    DELETE /api/v1/notes/{id}
    POST /api/v1/notes/{id}/read-aloud  -> {audioUrl}  (OpenAI TTS, cached)
    GET  /api/v1/notes/{id}/quiz
    POST /api/v1/notes/quiz/{quizId}/submit
"""

import asyncio
import json
from datetime import datetime, timezone
from typing import Any, Optional

from beanie import PydanticObjectId
from fastapi import APIRouter, Body, Depends, HTTPException, Query
from pydantic import BaseModel
from sse_starlette.sse import EventSourceResponse

from app.agents.note_agent import (
    answer_question, classify_intent, edit_note, generate_note,
)
from app.core.deps import get_current_user
from app.models.note import Note
from app.models.quiz import Quiz
from app.models.user import User
from app.services import media, openai_speech
from app.services.note_speech import note_to_speech, segment_note

router = APIRouter(prefix="/api/v1/notes", tags=["notes"])


class GenerateRequest(BaseModel):
    prompt: str
    images: Optional[list[str]] = None  # data URLs for multimodal input


class EditRequest(BaseModel):
    instruction: str
    sourceVersion: Optional[int] = None


class AssistRequest(BaseModel):
    prompt: str
    noteId: Optional[str] = None
    images: Optional[list[str]] = None
    sourceVersion: Optional[int] = None


def _note_dict(note: Note) -> dict[str, Any]:
    d = note.model_dump(mode="json")
    d["id"] = str(note.id)
    d.pop("userId", None)
    if not d.get("versions"):
        d["versions"] = [{
            "number": 1,
            "content": note.content,
            "images": [image.model_dump(mode="json") for image in note.images],
            "instruction": "Original note",
            "basedOnVersion": None,
            "quizId": str(note.quizId) if note.quizId else None,
            "createdAt": note.createdAt.isoformat(),
        }]
    history = d.get("chatHistory") or []
    # Backfill chat links in the response for notes created before chat turns
    # stored versionNumber. Match by the user's generating instruction first,
    # then fall back to the earliest still-unlinked assistant reply.
    ordered_versions = sorted(d["versions"], key=lambda item: item["number"])
    # First pass: exact instruction-to-reply matches. Do this before any fallback
    # so an old Version 2 reply is never accidentally labelled Version 1.
    for version in ordered_versions:
        number = version["number"]
        if any(turn.get("versionNumber") == number for turn in history):
            continue
        instruction = (version.get("instruction") or "").strip()
        if instruction and instruction != "Original note":
            for index, turn in enumerate(history[:-1]):
                if turn.get("role") == "user" and (turn.get("message") or "").strip() == instruction:
                    following = history[index + 1]
                    if following.get("role") == "assistant" and not following.get("versionNumber"):
                        following["versionNumber"] = number
                        break

    # Second pass: pair any remaining historical assistant replies in order. If
    # a very old note has fewer stored replies than versions, add a harmless
    # response-only link so every saved version remains reachable from chat.
    for version in ordered_versions:
        number = version["number"]
        if any(turn.get("versionNumber") == number for turn in history):
            continue
        candidate = next((
            turn for turn in history
            if turn.get("role") == "assistant" and not turn.get("versionNumber")
        ), None)
        if candidate:
            candidate["versionNumber"] = number
        else:
            history.append({
                "role": "assistant",
                "message": f"Your saved Version {number} is available here.",
                "versionNumber": number,
            })
    return d


def _quiz_dict(quiz: Quiz) -> dict[str, Any]:
    return {
        "id": str(quiz.id),
        # Correct answers stay server-side; marking happens on submit.
        "questions": [{"question": q.question, "options": q.options} for q in quiz.questions],
        "score": quiz.score,
        "status": quiz.status,
        "attempts": [a.model_dump(mode="json") for a in (quiz.attempts or [])],
    }


async def _sse(run) -> EventSourceResponse:
    """Wrap an agent run (taking an async `emit`) as an SSE stream."""
    queue: asyncio.Queue = asyncio.Queue()

    async def emit(ev: dict[str, Any]) -> None:
        await queue.put(ev)

    async def runner() -> None:
        try:
            await run(emit)
        except Exception as exc:  # noqa: BLE001
            await queue.put({"type": "error", "message": str(exc)})
        finally:
            await queue.put({"type": "__end__"})

    async def gen():
        task = asyncio.create_task(runner())
        try:
            while True:
                ev = await queue.get()
                if ev.get("type") == "__end__":
                    break
                yield {"data": json.dumps(ev)}
        finally:
            await task

    return EventSourceResponse(gen())


@router.post("/generate")
async def generate(body: GenerateRequest, current: User = Depends(get_current_user)):
    if not body.prompt.strip():
        raise HTTPException(status_code=400, detail="Please describe the note you want.")
    onboarding = current.onboarding.model_dump(by_alias=True)

    async def run(emit):
        await generate_note(
            user_id=current.id, prompt=body.prompt.strip(),
            onboarding=onboarding, image_data_urls=body.images, emit=emit,
        )

    return await _sse(run)


@router.post("/{note_id}/edit")
async def edit(note_id: str, body: EditRequest, current: User = Depends(get_current_user)):
    note = await _owned_note(note_id, current)

    async def run(emit):
        await edit_note(
            note=note, instruction=body.instruction,
            source_version=body.sourceVersion, emit=emit,
        )

    return await _sse(run)


@router.post("/assist")
async def assist(body: AssistRequest, current: User = Depends(get_current_user)):
    """Single smart entry point: decides whether to generate a new note, edit the
    open note, or answer a question — then streams the result over SSE."""
    if not body.prompt.strip():
        raise HTTPException(status_code=400, detail="Please type something.")
    note = None
    if body.noteId and PydanticObjectId.is_valid(body.noteId):
        note = await Note.get(PydanticObjectId(body.noteId))
        if note and note.userId != current.id:
            note = None
    onboarding = current.onboarding.model_dump(by_alias=True)

    async def run(emit):
        if body.images:
            intent = "edit" if note else "generate"
        else:
            intent = await classify_intent(
                body.prompt.strip(), note_open=bool(note), note_title=note.title if note else ""
            )
        await emit({"type": "intent", "intent": intent})
        if intent == "generate":
            await generate_note(user_id=current.id, prompt=body.prompt.strip(),
                                onboarding=onboarding, image_data_urls=body.images, emit=emit)
        elif intent == "edit" and note:
            await edit_note(
                note=note, instruction=body.prompt.strip(),
                source_version=body.sourceVersion, emit=emit,
            )
        else:
            await answer_question(prompt=body.prompt.strip(), note=note,
                                  onboarding=onboarding, emit=emit)

    return await _sse(run)


@router.get("")
async def list_notes(current: User = Depends(get_current_user)) -> dict:
    notes = await Note.find(Note.userId == current.id).sort(-Note.createdAt).to_list()
    return {
        "success": True,
        "data": [
            {"id": str(n.id), "title": n.title, "tag": n.tag, "subject": n.subject,
             "topic": n.topic, "createdAt": n.createdAt.isoformat() if n.createdAt else None,
             "hasQuiz": n.quizId is not None, "imageCount": len(n.images)}
            for n in notes
        ],
    }


@router.get("/{note_id}")
async def get_note(note_id: str, current: User = Depends(get_current_user)) -> dict:
    note = await _owned_note(note_id, current)
    quiz = await Quiz.get(note.quizId) if note.quizId else None
    data = _note_dict(note)
    if quiz:
        data["quiz"] = _quiz_dict(quiz)
    for version in data["versions"]:
        version_quiz_id = version.get("quizId")
        if version_quiz_id and PydanticObjectId.is_valid(version_quiz_id):
            version_quiz = await Quiz.get(PydanticObjectId(version_quiz_id))
            if version_quiz:
                version["quiz"] = _quiz_dict(version_quiz)
    return {"success": True, "data": data}


@router.put("/{note_id}/visibility")
async def toggle_visibility(note_id: str, current: User = Depends(get_current_user)) -> dict:
    note = await _owned_note(note_id, current)
    note.isPublic = not note.isPublic
    await note.save()
    return {"success": True, "data": {"isPublic": note.isPublic}}


@router.delete("/{note_id}")
async def delete_note(note_id: str, current: User = Depends(get_current_user)) -> dict:
    note = await _owned_note(note_id, current)
    quiz_ids = {note.quizId} if note.quizId else set()
    quiz_ids.update(version.quizId for version in note.versions if version.quizId)
    for quiz_id in quiz_ids:
        q = await Quiz.get(quiz_id)
        if q:
            await q.delete()
    await note.delete()
    return {"success": True, "message": "Note deleted"}


def _version_content(note: Note, requested: int | None) -> tuple[int, str]:
    """Resolve narration to the version currently visible in the frontend."""
    version_number = requested or note.currentVersion or 1
    if requested is not None:
        version = next((item for item in note.versions if item.number == requested), None)
        # Legacy notes may not yet have their first immutable snapshot. Their
        # top-level content is still Version 1/current and remains narratable.
        if version is None and requested == (note.currentVersion or 1):
            return requested, note.content
        if version is None:
            raise HTTPException(status_code=404, detail="Note version not found")
        return version.number, version.content
    return version_number, note.content


@router.post("/{note_id}/read-aloud")
async def read_aloud(
    note_id: str,
    version: int | None = Query(default=None, ge=1),
    current: User = Depends(get_current_user),
) -> dict:
    note = await _owned_note(note_id, current)
    version_number, content = _version_content(note, version)
    provider = openai_speech.provider_key()
    if (note.audioUrl and note.audioProvider == provider
            and note.audioVersion == version_number):
        return {"success": True, "data": {"audioUrl": note.audioUrl, "cached": True}}
    if not openai_speech.available():
        raise HTTPException(status_code=503, detail="Read-aloud is not configured.")
    text = note_to_speech(content)  # maths→words, natural pauses, image descriptions
    audio = await openai_speech.text_to_speech(text)
    if not audio:
        raise HTTPException(status_code=502, detail="Could not generate audio right now.")
    url = media.upload_audio(audio, public_id=f"note_{note.id}_v{version_number}")
    if not url:
        raise HTTPException(status_code=502, detail="Could not store audio.")
    note.audioUrl = url
    note.audioProvider = provider
    note.audioVersion = version_number
    await note.save()
    return {"success": True, "data": {"audioUrl": url, "cached": False}}


@router.post("/{note_id}/read-along")
async def read_along(
    note_id: str,
    version: int | None = Query(default=None, ge=1),
    current: User = Depends(get_current_user),
) -> dict:
    """Return ordered, spoken segments each with its own audio, for the
    highlight-and-auto-scroll player. Cached on the note after first build."""
    note = await _owned_note(note_id, current)
    version_number, content = _version_content(note, version)
    provider = openai_speech.provider_key()
    # Provider-tagged caches prevent older Spitch clips from being reused.
    cached = note.readAlong
    if (cached and cached.get("provider") == provider
            and cached.get("version") == version_number and cached.get("segments")
            and any(s.get("audioUrl") for s in cached["segments"])):
        return {"success": True, "data": cached}
    if not openai_speech.available():
        raise HTTPException(status_code=503, detail="Read-aloud is not configured.")

    segments = segment_note(content)
    if not segments:
        raise HTTPException(status_code=400, detail="Nothing to read yet.")

    sem = asyncio.Semaphore(4)  # bounded concurrency for TTS + upload

    async def build(seg: dict) -> dict:
        async with sem:
            audio = await openai_speech.text_to_speech(seg["spoken"])
            public_id = f"note_{note.id}_v{version_number}_seg_{seg['index']}"
            url = media.upload_audio(audio, public_id=public_id) if audio else None
        return {**seg, "audioUrl": url}

    built = await asyncio.gather(*(build(s) for s in segments))
    built.sort(key=lambda s: s["index"])
    data = {"provider": provider, "version": version_number, "segments": built}
    # Only persist the cache if we actually produced audio; otherwise leave it
    # unset so a later call retries once credits are available.
    if any(s.get("audioUrl") for s in built):
        note.readAlong = data
        await note.save()
    return {"success": True, "data": data}


@router.get("/{note_id}/quiz")
async def get_quiz(note_id: str, current: User = Depends(get_current_user)) -> dict:
    note = await _owned_note(note_id, current)
    if not note.quizId:
        raise HTTPException(status_code=404, detail="No quiz for this note")
    quiz = await Quiz.get(note.quizId)
    if not quiz:
        raise HTTPException(status_code=404, detail="Quiz not found")
    # Do not leak correct answers up front.
    return {
        "success": True,
        "data": {
            "id": str(quiz.id),
            "questions": [{"question": q.question, "options": q.options} for q in quiz.questions],
        },
    }


@router.post("/quiz/{quiz_id}/submit")
async def submit_quiz(
    quiz_id: str,
    answers: list[str] = Body(..., embed=True),
    current: User = Depends(get_current_user),
) -> dict:
    if not PydanticObjectId.is_valid(quiz_id):
        raise HTTPException(status_code=400, detail="Invalid quiz id")
    quiz = await Quiz.get(PydanticObjectId(quiz_id))
    if not quiz or quiz.userId != current.id:
        raise HTTPException(status_code=404, detail="Quiz not found")

    results = []
    score = 0
    for i, q in enumerate(quiz.questions):
        given = answers[i] if i < len(answers) else None
        correct = (given is not None and str(given).strip() == str(q.correctAnswer).strip())
        if correct:
            score += 1
        results.append({
            "question": q.question, "correctAnswer": q.correctAnswer,
            "yourAnswer": given, "isCorrect": correct, "explanation": q.explanation,
        })
    from app.models.quiz import QuizAttempt
    quiz.score = score
    quiz.status = "completed"
    quiz.attempts = (quiz.attempts or []) + [
        QuizAttempt(score=score, total=len(quiz.questions), at=datetime.now(timezone.utc))
    ]
    await quiz.save()

    from app.models.activity import Activity
    await Activity(userId=current.id, type="quiz_study", status="completed",
                   endTime=datetime.now(timezone.utc),
                   metadata={"quizId": quiz_id, "score": score, "total": len(quiz.questions)}).insert()

    return {"success": True, "data": {"score": score, "total": len(quiz.questions), "results": results}}


async def _owned_note(note_id: str, user: User) -> Note:
    if not PydanticObjectId.is_valid(note_id):
        raise HTTPException(status_code=400, detail="Invalid note id")
    note = await Note.get(PydanticObjectId(note_id))
    if not note or note.userId != user.id:
        raise HTTPException(status_code=404, detail="Note not found")
    return note
