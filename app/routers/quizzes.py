"""Quiz library endpoints — ported from Node.

    GET  /api/v1/quizzes              user's quizzes
    GET  /api/v1/quizzes/{id}         one quiz (no answers)
    POST /api/v1/quizzes/explain      AI explanation for a quiz question
    POST /api/v1/quizzes/{id}/submit  mark + record attempt (same engine as notes)
"""

from typing import Any, Optional

from beanie import PydanticObjectId
from fastapi import APIRouter, Body, Depends, HTTPException
from pydantic import BaseModel

from app.core.deps import get_current_user
from app.core.llm import chat
from app.models.quiz import Quiz
from app.models.user import User
from app.routers.notes import submit_quiz as _notes_submit_quiz

router = APIRouter(prefix="/api/v1/quizzes", tags=["quizzes"])


class ExplainRequest(BaseModel):
    question: str
    options: Optional[list[str]] = None
    correctAnswer: Optional[str] = None
    userAnswer: Optional[str] = None


@router.get("")
async def list_quizzes(current: User = Depends(get_current_user)) -> dict:
    quizzes = await Quiz.find(Quiz.userId == current.id).sort(-Quiz.createdAt).to_list()
    data = [{
        "_id": str(q.id), "id": str(q.id), "noteTitle": q.noteTitle,
        "noteId": str(q.noteId) if q.noteId else None,
        "questionCount": len(q.questions or []), "score": q.score, "status": q.status,
        "attempts": len(q.attempts or []),
        "createdAt": q.createdAt.isoformat() if q.createdAt else None,
    } for q in quizzes]
    return {"success": True, "count": len(data), "data": data}


@router.get("/{quiz_id}")
async def get_quiz(quiz_id: str, current: User = Depends(get_current_user)) -> dict:
    quiz = await _owned(quiz_id, current)
    return {"success": True, "data": {
        "_id": str(quiz.id), "id": str(quiz.id), "noteTitle": quiz.noteTitle,
        "questions": [{"question": q.question, "options": q.options} for q in quiz.questions],
        "score": quiz.score, "status": quiz.status,
        "attempts": [a.model_dump(mode="json") for a in (quiz.attempts or [])],
    }}


@router.post("/explain")
async def explain(body: ExplainRequest, current: User = Depends(get_current_user)) -> dict:
    user_class = current.onboarding.class_ or "WAEC/JAMB"
    explanation = await chat(
        [{"role": "system", "content": "You are a warm Nigerian tutor. Clean Markdown, LaTeX ($...$) for maths, simple words."},
         {"role": "user", "content": f"""Explain this quiz question to a {user_class} student, simply:

QUESTION: {body.question}
{f"OPTIONS: {body.options}" if body.options else ""}
{f"CORRECT ANSWER: {body.correctAnswer}" if body.correctAnswer else ""}
{f"STUDENT'S ANSWER: {body.userAnswer}" if body.userAnswer else ""}

Explain why the correct answer is right{', why theirs was wrong,' if body.userAnswer else ''} and the concept behind it."""}],
        tier="default", temperature=0.5,
    )
    return {"success": True, "explanation": explanation}


@router.post("/{quiz_id}/submit")
async def submit(
    quiz_id: str,
    answers: list[Any] = Body(..., embed=True),
    current: User = Depends(get_current_user),
) -> dict:
    # Same marking engine (attempts, activity, score) as the notes flow.
    return await _notes_submit_quiz(quiz_id, answers=answers, current=current)


async def _owned(quiz_id: str, user: User) -> Quiz:
    if not PydanticObjectId.is_valid(quiz_id):
        raise HTTPException(status_code=400, detail="Invalid quiz id")
    quiz = await Quiz.get(PydanticObjectId(quiz_id))
    if not quiz or quiz.userId != user.id:
        raise HTTPException(status_code=404, detail="Quiz not found")
    return quiz
