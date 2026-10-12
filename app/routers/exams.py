"""Exam endpoints — ported from Node (same shapes, Python engine).

    POST /api/v1/exams/generate              build an exam from the question bank
    POST /api/v1/exams/{id}/submit-objective mark objective answers + AI feedback on misses
    POST /api/v1/exams/{id}/submit-theory    AI-mark theory answers
    GET  /api/v1/exams/history               past exams (questions populated)
"""

from datetime import datetime, timezone
from typing import Any, Optional

from beanie import PydanticObjectId
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.core.deps import get_current_user
from app.core.llm import chat_json
from app.models.activity import Activity
from app.models.exam import Exam, ExamQuestion
from app.models.question import Question
from app.models.user import User

router = APIRouter(prefix="/api/v1/exams", tags=["exams"])


class GenerateExamRequest(BaseModel):
    subject: str
    exam_name: str
    question_type: str
    year: Optional[int] = None
    size: int = 50


class SubmitRequest(BaseModel):
    answers: list[dict[str, Any]]  # [{questionId, userAnswer}]


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def _populated(exam: Exam) -> dict[str, Any]:
    """Serialise an exam with each questionId expanded to the full question doc
    (mirrors Mongoose .populate('questions.questionId'))."""
    ids = [eq.questionId for eq in exam.questions if eq.questionId]
    docs = {q.id: q for q in await Question.find({"_id": {"$in": ids}}).to_list()}
    out = exam.model_dump(mode="json")
    out["_id"] = str(exam.id)
    for i, eq in enumerate(exam.questions):
        qdoc = docs.get(eq.questionId)
        out["questions"][i]["questionId"] = (
            {**qdoc.model_dump(mode="json"), "_id": str(qdoc.id)} if qdoc else None
        )
    return out


@router.post("/generate")
async def generate_exam(body: GenerateExamRequest, current: User = Depends(get_current_user)) -> dict:
    match: dict[str, Any] = {
        "subject": body.subject.lower(),
        "exam_name": body.exam_name.upper(),
        "question_type": body.question_type.lower(),
    }
    if body.year:
        match["year"] = body.year

    size = max(1, body.size)
    docs = await Question.aggregate([{"$match": match}, {"$sample": {"size": size}}]).to_list()
    if not docs:
        raise HTTPException(status_code=404, detail="No questions found for the selected criteria")

    exam = Exam(
        userId=current.id,
        subject=body.subject.lower(),
        exam_name=body.exam_name.upper(),
        question_type=body.question_type.lower(),
        questions=[ExamQuestion(questionId=d["_id"]) for d in docs],
        totalQuestions=len(docs),
        status="ongoing",
    )
    await exam.insert()

    await Activity(
        userId=current.id, type="past_question_study", status="active",
        startTime=_now(), metadata={"examId": str(exam.id), "subject": exam.subject},
    ).insert()

    return {"success": True, "data": await _populated(exam)}


@router.post("/{exam_id}/submit-objective")
async def submit_objective(exam_id: str, body: SubmitRequest, current: User = Depends(get_current_user)) -> dict:
    exam = await _owned_exam(exam_id, current, "obj")
    if exam.status == "completed":
        raise HTTPException(status_code=400, detail="Exam has already been submitted")

    ids = [eq.questionId for eq in exam.questions if eq.questionId]
    qdocs = {str(q.id): q for q in await Question.find({"_id": {"$in": ids}}).to_list()}
    given = {a.get("questionId"): a.get("userAnswer") for a in body.answers}

    score = 0
    wrong: list[dict[str, Any]] = []
    for eq in exam.questions:
        qid = str(eq.questionId)
        q = qdocs.get(qid)
        if qid in given and q:
            eq.userAnswer = given[qid]
            if given[qid] == q.correct_answer:
                eq.isCorrect = True
                score += 1
            else:
                eq.isCorrect = False
                wrong.append({"id": qid, "text": q.question_text, "options": q.options,
                              "correct": q.correct_answer, "user": eq.userAnswer})

    if wrong:
        try:
            feedback = await chat_json(
                [{"role": "system", "content": "You are a warm, expert Nigerian tutor."},
                 {"role": "user", "content": f"""The student got these {exam.subject} objective questions wrong.
For each, give: "wrongReason" (why their answer is wrong, briefly) and "correctExplanation" (the concept,
explained simply with LaTeX $...$ for maths).

QUESTIONS: {wrong}

Return ONLY a JSON object: keys are question IDs, values are {{"wrongReason", "correctExplanation"}}."""}],
                tier="default", temperature=0.4,
            )
            if isinstance(feedback, dict):
                for eq in exam.questions:
                    fb = feedback.get(str(eq.questionId))
                    if fb:
                        eq.aiFeedback = fb
        except Exception:  # noqa: BLE001 — marking must still complete without feedback
            pass

    exam.score = score
    exam.status = "completed"
    exam.completedAt = _now()
    await exam.save()
    await _stop_activity(current, exam)
    return {"success": True, "data": await _populated(exam)}


@router.post("/{exam_id}/submit-theory")
async def submit_theory(exam_id: str, body: SubmitRequest, current: User = Depends(get_current_user)) -> dict:
    exam = await _owned_exam(exam_id, current, "theory")
    if exam.status == "completed":
        raise HTTPException(status_code=400, detail="Exam has already been submitted")

    ids = [eq.questionId for eq in exam.questions if eq.questionId]
    qdocs = {str(q.id): q for q in await Question.find({"_id": {"$in": ids}}).to_list()}
    given = {a.get("questionId"): a.get("userAnswer") for a in body.answers}

    theory: list[dict[str, Any]] = []
    for eq in exam.questions:
        qid = str(eq.questionId)
        if qid in given and qdocs.get(qid):
            eq.userAnswer = given[qid]
            theory.append({"id": qid, "text": qdocs[qid].question_text, "user": eq.userAnswer})

    total_score = 0.0
    if theory:
        try:
            marking = await chat_json(
                [{"role": "system", "content": "You are a fair, encouraging Nigerian examiner."},
                 {"role": "user", "content": f"""Mark these {exam.subject} theory answers. For each: is it correct
(full marks), partially correct, or incorrect? Give constructive feedback and the ideal model answer
(simple language, LaTeX $...$ for maths).

ANSWERS: {theory}

Return ONLY a JSON object: keys are question IDs, values are
{{"isCorrect": bool, "score": number 0-1, "feedback": str, "modelAnswer": str}}."""}],
                tier="smart", temperature=0.3,
            )
            if isinstance(marking, dict):
                for eq in exam.questions:
                    r = marking.get(str(eq.questionId))
                    if r:
                        eq.isCorrect = bool(r.get("isCorrect"))
                        eq.aiFeedback = r
                        if eq.isCorrect:
                            total_score += float(r.get("score", 1) or 1)
        except Exception:  # noqa: BLE001
            pass

    exam.score = int(round(total_score))
    exam.status = "completed"
    exam.completedAt = _now()
    await exam.save()
    await _stop_activity(current, exam)
    return {"success": True, "data": await _populated(exam)}


@router.get("/history")
async def exam_history(current: User = Depends(get_current_user)) -> dict:
    exams = await Exam.find(Exam.userId == current.id).sort(-Exam.createdAt).to_list()
    data = [await _populated(e) for e in exams]
    return {"success": True, "count": len(data), "data": data}


async def _owned_exam(exam_id: str, user: User, qtype: str) -> Exam:
    if not PydanticObjectId.is_valid(exam_id):
        raise HTTPException(status_code=400, detail="Invalid exam id")
    exam = await Exam.get(PydanticObjectId(exam_id))
    if not exam or exam.userId != user.id or exam.question_type != qtype:
        raise HTTPException(status_code=404, detail=f"{qtype.title()} exam session not found")
    return exam


async def _stop_activity(user: User, exam: Exam) -> None:
    activity = await Activity.find(
        Activity.userId == user.id, Activity.type == "past_question_study", Activity.status == "active",
    ).sort(-Activity.startTime).first_or_none()
    if not activity or (activity.metadata or {}).get("examId") != str(exam.id):
        return
    end = _now()
    start = activity.startTime
    if start and start.tzinfo is None:
        start = start.replace(tzinfo=timezone.utc)
    duration = max(0, int((end - start).total_seconds())) if start else 0
    activity.endTime = end
    activity.duration = duration
    activity.status = "completed"
    await activity.save()

    stats = user.studyStats
    stats.totalStudyTime = int(stats.totalStudyTime or 0) + duration
    stats.lastActivity = end
    stats.activityCounts.past_question_study = int(stats.activityCounts.past_question_study or 0) + 1
    await user.save()
