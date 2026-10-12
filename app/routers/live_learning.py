"""Live Learning classroom endpoints.

Flow: POST /prepare (form -> notebook written in the background) -> GET
/{id}/notebook (the preparing screen polls progress) -> POST /session (voice
starts, briefed with the whole notebook) -> /page, /ops, /quiz-result while
teaching -> /pause or /complete.
"""

import asyncio
import logging
from datetime import UTC, datetime
from typing import Literal

from beanie import PydanticObjectId
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from app.core.deps import get_current_user
from app.models.activity import Activity
from app.models.live_lesson import BoardOperation, LessonTranscript, LiveLesson, NoteLine
from app.models.quiz import Quiz, QuizAttempt, QuizQuestion
from app.models.user import User
from app.services import lesson_pack, live_learning, notebook

router = APIRouter(prefix="/api/v1/live-learning", tags=["live-learning"])
logger = logging.getLogger("studymaster.live_learning")

# Keep references so background notebook builds are not garbage-collected.
_BUILDS: set[asyncio.Task] = set()


async def _owned_lesson(lesson_id: str, user: User) -> LiveLesson:
    if not PydanticObjectId.is_valid(lesson_id):
        raise HTTPException(status_code=400, detail="Invalid lesson id")
    lesson = await LiveLesson.get(PydanticObjectId(lesson_id))
    if not lesson or lesson.userId != user.id:
        raise HTTPException(status_code=404, detail="Lesson not found")
    return lesson


async def _patch(lesson: LiveLesson, *, set_: dict | None = None,
                 push: dict[str, list] | None = None) -> None:
    """Update ONLY the named fields of a lesson.

    During a class several requests overlap. ``lesson.save()`` rewrites the
    whole document, so a slower request would overwrite a newer one (that is
    how a plan was once wiped mid-lesson). Targeted $set/$push cannot clobber.
    """
    def plain(value):  # pydantic models -> plain dicts for Mongo
        if isinstance(value, list):
            return [plain(v) for v in value]
        return value.model_dump() if hasattr(value, "model_dump") else value

    update: dict = {"$set": {"updatedAt": datetime.now(UTC), **{k: plain(v) for k, v in (set_ or {}).items()}}}
    if push:
        update["$push"] = {k: {"$each": plain(v)} for k, v in push.items() if v}
    await LiveLesson.get_pymongo_collection().update_one({"_id": lesson.id}, update)


# ── 1. Prepare: the form becomes a full notebook ────────────────────────
class PrepareRequest(BaseModel):
    subject: str = Field(min_length=2, max_length=100)
    topic: str = Field(min_length=2, max_length=180)
    # Required: the notes are shaped around what the student says here.
    details: str = Field(min_length=10, max_length=1200)
    goal: str = Field(default="Understand the topic", max_length=240)
    startingLevel: str = Field(default="new", max_length=40)
    durationMinutes: int = Field(default=30, ge=5, le=120)


async def _build_notebook(lesson: LiveLesson) -> None:
    try:
        nb = await notebook.build(
            lesson_id=str(lesson.id), subject=lesson.subject, topic=lesson.topic, goal=lesson.goal,
            level=lesson.startingLevel, details=lesson.details, minutes=lesson.targetMinutes,
        )
    except Exception:
        logger.exception("notebook build crashed")
        nb = {}
    if nb.get("pages"):
        await _patch(lesson, set_={"notebook": nb, "status": "ready", "currentStepIndex": 0})
    else:
        await _patch(lesson, set_={"status": "failed"})


@router.post("/prepare")
async def prepare(body: PrepareRequest, current: User = Depends(get_current_user)) -> dict:  # noqa: B008
    lesson = LiveLesson(
        userId=current.id, subject=body.subject.strip(), topic=body.topic.strip(),
        goal=body.goal.strip() or "Understand the topic", details=body.details.strip(),
        startingLevel=body.startingLevel, targetMinutes=body.durationMinutes, status="preparing",
    )
    await lesson.insert()
    task = asyncio.create_task(_build_notebook(lesson))
    _BUILDS.add(task)
    task.add_done_callback(_BUILDS.discard)
    return {"success": True, "data": {"lessonId": str(lesson.id)}}


@router.get("/{lesson_id}/notebook")
async def get_notebook(lesson_id: str, current: User = Depends(get_current_user)) -> dict:  # noqa: B008
    """What the preparing screen polls: progress while writing, the notebook when ready."""
    lesson = await _owned_lesson(lesson_id, current)
    nb = getattr(lesson, "notebook", None) or {}
    progress = notebook.PROGRESS.get(lesson_id, {"stage": "Starting", "done": 0, "total": 1})
    status = "ready" if nb.get("pages") else ("failed" if lesson.status == "failed" else "preparing")
    if status == "preparing" and lesson_id not in notebook.PROGRESS and lesson.status == "preparing":
        # the server restarted mid-build: the background task is gone
        status = "failed"
    return {"success": True, "data": {
        "status": status, "progress": progress,
        "notebook": nb if status == "ready" else None,
        "currentPage": lesson.currentStepIndex,
    }}


# ── 2. The live session: voice briefed with the whole notebook ─────────
class SessionRequest(BaseModel):
    sdp: str = Field(min_length=1)
    lessonId: str


@router.post("/session")
async def create_session(body: SessionRequest, current: User = Depends(get_current_user)) -> dict:  # noqa: B008
    lesson = await _owned_lesson(body.lessonId, current)
    nb = getattr(lesson, "notebook", None) or {}
    pages = nb.get("pages") or []
    if not pages:
        raise HTTPException(status_code=409, detail="The lesson notes are not ready yet")
    resumed = lesson.status in {"paused", "active"}
    page = min(lesson.currentStepIndex, len(pages) - 1)
    instructions = live_learning.teacher_instructions(
        name=current.username, subject=lesson.subject, topic=lesson.topic, goal=lesson.goal,
        details=lesson.details, level=lesson.startingLevel, notes=notebook.notebook_text(pages),
        pages=len(pages), has_quiz=bool(nb.get("hasQuiz")),
        resume_page=page if resumed and page > 0 else None,
    )
    try:
        live = await live_learning.create_live_session(
            sdp=body.sdp, instructions=instructions,
            delegation=live_learning.delegation_config(
                subject=lesson.subject, topic=lesson.topic, page_titles=[p["title"] for p in pages]),
        )
    except RuntimeError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    await _patch(lesson, set_={"openaiSessionId": live.get("session", {}).get("id"), "status": "active"})
    await Activity(
        userId=current.id, type="live_teach", status="active",
        metadata={"lessonId": str(lesson.id), "subject": lesson.subject, "topic": lesson.topic},
    ).insert()
    return {"success": True, "data": {
        "lessonId": str(lesson.id), "sdp": live.get("transport", {}).get("sdp"),
        "resumed": resumed, "elapsedSeconds": lesson.elapsedSeconds, "currentPage": page,
        "notebook": nb,
        # extra lines written during earlier sessions of this lesson
        "boardOperations": [op.model_dump(mode="json") for op in lesson.boardOperations],
    }}


# ── 3. While teaching ───────────────────────────────────────────────────
class PageRequest(BaseModel):
    page: int = Field(ge=0, le=50)


@router.post("/{lesson_id}/page")
async def set_page(lesson_id: str, body: PageRequest,
                   current: User = Depends(get_current_user)) -> dict:  # noqa: B008
    """Remember the page the board is on, so a resumed lesson opens there."""
    lesson = await _owned_lesson(lesson_id, current)
    await _patch(lesson, set_={"currentStepIndex": body.page})
    return {"success": True, "data": {"page": body.page}}


class ClientOp(BaseModel):
    """An extra board line the browser has ALREADY drawn (not part of the notes)."""

    id: str = Field(min_length=1, max_length=64)
    action: Literal[
        "set_focus", "write_heading", "write_text", "write_formula",
        "write_steps", "show_example", "show_question", "highlight_item",
        "clear_board", "new_page", "show_image",
    ]
    content: dict = Field(default_factory=dict)
    stepIndex: int = Field(default=0, ge=0, le=50)
    emphasis: Literal["normal", "key"] = "normal"


class OpsRequest(BaseModel):
    ops: list[ClientOp] = Field(min_length=1, max_length=20)


@router.post("/{lesson_id}/ops")
async def save_ops(lesson_id: str, body: OpsRequest,
                   current: User = Depends(get_current_user)) -> dict:  # noqa: B008
    lesson = await _owned_lesson(lesson_id, current)
    known = {op.id for op in lesson.boardOperations}
    new_ops: list[BoardOperation] = []
    new_notes: list[NoteLine] = []
    for item in body.ops:
        if item.id in known:
            continue
        new_ops.append(BoardOperation(
            id=item.id, revision=len(lesson.boardOperations) + len(new_ops), action=item.action,
            content=item.content, stepIndex=item.stepIndex, emphasis=item.emphasis,
        ))
        text = item.content.get("text") or item.content.get("latex") or ""
        if item.action in {"write_text", "write_formula", "write_heading"} and text:
            new_notes.append(NoteLine(
                text=str(text)[:600],
                kind=("heading" if item.action == "write_heading"
                      else "formula" if item.action == "write_formula"
                      else "key" if item.emphasis == "key" else "point"),
                latex=str(item.content.get("latex", ""))[:600], stepIndex=item.stepIndex,
            ))
    await _patch(lesson, push={"boardOperations": new_ops, "notes": new_notes})
    return {"success": True, "data": {"saved": len(new_ops)}}


@router.get("/image")
async def find_image(query: str, current: User = Depends(get_current_user)) -> dict:  # noqa: B008
    """One relevance-checked picture for an improvised board moment."""
    picture = await lesson_pack.pick_image(query[:80], purpose=query[:80])
    return {"success": True, "data": picture}


class QuizAnswer(BaseModel):
    index: int = Field(ge=0, le=20)
    picked: int = Field(ge=0, le=3)
    attempts: int = Field(default=1, ge=1, le=4)


class QuizResultRequest(BaseModel):
    answers: list[QuizAnswer] = Field(min_length=1, max_length=20)


@router.post("/{lesson_id}/quiz-result")
async def quiz_result(lesson_id: str, body: QuizResultRequest,
                      current: User = Depends(get_current_user)) -> dict:  # noqa: B008
    """Record the end-of-lesson quiz. A first-try correct answer scores; the
    result is saved as a completed Quiz so it feeds the readiness estimate."""
    lesson = await _owned_lesson(lesson_id, current)
    pages = (getattr(lesson, "notebook", None) or {}).get("pages") or []
    questions = [op["content"] for p in pages if p.get("title") == "Quiz" for op in p["ops"]]
    if not questions:
        raise HTTPException(status_code=400, detail="This lesson has no quiz")
    first_try = {a.index: a for a in body.answers}
    score = sum(1 for i, q in enumerate(questions)
                if i in first_try and first_try[i].attempts == 1 and first_try[i].picked == q["correctIndex"])
    now = datetime.now(UTC)
    letters = "ABCD"
    await Quiz(
        userId=current.id, noteTitle=f"Live lesson: {lesson.topic}", isPublic=False,
        questions=[QuizQuestion(question=q["question"], options=q["options"],
                                correctAnswer=letters[q["correctIndex"]],
                                explanation=(q.get("explanations") or [""])[q["correctIndex"]]
                                if len(q.get("explanations") or []) > q["correctIndex"] else "")
                   for q in questions],
        score=score, status="completed",
        attempts=[QuizAttempt(score=score, total=len(questions), at=now)],
        liveLessonId=str(lesson.id),
    ).insert()
    await _patch(lesson, set_={"quizResult": {"score": score, "total": len(questions), "at": now}})
    return {"success": True, "data": {"score": score, "total": len(questions)}}


class CompleteRequest(BaseModel):
    elapsedSeconds: int = Field(default=0, ge=0, le=43200)
    transcript: list[dict] = Field(default_factory=list)


@router.post("/{lesson_id}/extend")
async def extend_lesson(lesson_id: str, minutes: int = 15,
                        current: User = Depends(get_current_user)) -> dict:  # noqa: B008
    lesson = await _owned_lesson(lesson_id, current)
    minutes = max(5, min(minutes, 60))
    lesson.targetMinutes += minutes
    await _patch(lesson, set_={"targetMinutes": lesson.targetMinutes}, push={"extensions": [minutes]})
    return {"success": True, "data": {"targetMinutes": lesson.targetMinutes}}


@router.post("/{lesson_id}/complete")
async def complete_lesson(lesson_id: str, body: CompleteRequest,
                          current: User = Depends(get_current_user)) -> dict:  # noqa: B008
    lesson = await _owned_lesson(lesson_id, current)
    said = [
        LessonTranscript(role=item.get("role"), text=str(item.get("text", "")).strip()[:3000])
        for item in body.transcript[-80:]
        if item.get("role") in {"user", "assistant"} and str(item.get("text", "")).strip()
    ]
    result = getattr(lesson, "quizResult", None) or {}
    lesson.summary = (f"Completed a {max(1, body.elapsedSeconds // 60)}-minute lesson on {lesson.topic}"
                      + (f" and scored {result['score']}/{result['total']} on the quiz." if result else "."))
    lesson.completedAt = datetime.now(UTC)
    await _patch(lesson, set_={
        "status": "completed", "elapsedSeconds": body.elapsedSeconds,
        "summary": lesson.summary, "completedAt": lesson.completedAt,
    }, push={"transcript": said})
    activity = await Activity.find_one({
        "userId": current.id, "type": "live_teach", "status": "active",
        "metadata.lessonId": str(lesson.id),
    })
    if activity:
        activity.status = "completed"
        activity.endTime = lesson.completedAt
        activity.duration = body.elapsedSeconds
        await activity.save()
    return {"success": True, "data": {"summary": lesson.summary}}


@router.get("/lessons")
async def list_lessons(current: User = Depends(get_current_user)) -> dict:  # noqa: B008
    """Past and paused lessons for the Live Learning tab, newest first.

    Declared BEFORE /{lesson_id} so the literal path wins the route match.
    """
    lessons = (
        await LiveLesson.find(LiveLesson.userId == current.id)
        .sort(-LiveLesson.createdAt)
        .limit(40)
        .to_list()
    )
    out = []
    for lesson in lessons:
        pages = (getattr(lesson, "notebook", None) or {}).get("pages") or []
        out.append({
            "id": str(lesson.id), "subject": lesson.subject, "topic": lesson.topic,
            "goal": lesson.goal, "details": lesson.details, "startingLevel": lesson.startingLevel,
            "status": lesson.status, "elapsedSeconds": lesson.elapsedSeconds,
            "targetMinutes": lesson.targetMinutes, "pageCount": len(pages),
            "currentPage": lesson.currentStepIndex, "summary": lesson.summary,
            # only lessons with written notes can be continued in the new flow
            "resumable": lesson.status in {"paused", "active", "ready"} and bool(pages),
            "createdAt": lesson.createdAt.isoformat() if lesson.createdAt else None,
            "updatedAt": (lesson.updatedAt or lesson.createdAt).isoformat()
            if (lesson.updatedAt or lesson.createdAt) else None,
        })
    return {"success": True, "data": out}


class PauseRequest(BaseModel):
    elapsedSeconds: int = Field(default=0, ge=0, le=43200)


@router.post("/{lesson_id}/pause")
async def pause_lesson(lesson_id: str, body: PauseRequest,
                       current: User = Depends(get_current_user)) -> dict:  # noqa: B008
    """Persist a paused lesson so the student can pick it up later."""
    lesson = await _owned_lesson(lesson_id, current)
    status = lesson.status if lesson.status in {"completed", "failed"} else "paused"
    elapsed = max(lesson.elapsedSeconds, body.elapsedSeconds)
    await _patch(lesson, set_={"status": status, "elapsedSeconds": elapsed})
    return {"success": True, "data": {"status": status, "elapsedSeconds": elapsed}}


@router.get("/{lesson_id}")
async def get_lesson(lesson_id: str, current: User = Depends(get_current_user)) -> dict:  # noqa: B008
    lesson = await _owned_lesson(lesson_id, current)
    data = lesson.model_dump(mode="json")
    data["id"] = str(lesson.id)
    return {"success": True, "data": data}
