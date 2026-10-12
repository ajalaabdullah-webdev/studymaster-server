"""V2 onboarding flow — exam pick -> career research -> subjects -> diagnostic.

Mounted at ``/api/v1/onboarding``. Each step persists partial progress on the
user so the flow can be resumed, and so the "old user, please re-onboard" pop-up
can tell how far someone got.
"""

from datetime import datetime, timezone

from beanie import PydanticObjectId
from fastapi import APIRouter, Depends, HTTPException

from app.agents.onboarding_agent import research_career
from app.core.deps import get_current_user
from app.core.subjects import normalize_many
from app.models.activity import Activity
from app.models.diagnostic import DiagnosticItem, DiagnosticSession
from app.models.user import User
from app.schemas.onboarding import (
    CareerResearchRequest,
    CareerResearchResponse,
    DiagnosticStartRequest,
    DiagnosticSubmitRequest,
    ExamsRequest,
    ProfileRequest,
    SubjectRequirement,
    SubjectsRequest,
)
from app.services.questions import pick_diagnostic_questions

router = APIRouter(prefix="/api/v1/onboarding", tags=["onboarding"])


def _now() -> datetime:
    return datetime.now(timezone.utc)


@router.get("/status")
async def onboarding_status(current: User = Depends(get_current_user)) -> dict:
    ob = current.onboarding
    return {
        "success": True,
        "data": {
            "onboardingCompleted": current.onboardingCompleted,
            "version": ob.version,
            "exams": ob.exams,
            "career": ob.career,
            "requiredSubjects": ob.requiredSubjects,
            "selectedSubjects": ob.selectedSubjects,
            "targetScores": ob.targetScores,
            # A V1 user who hasn't done V2 yet -> the frontend GATES them into
            # the new onboarding (it is not optional; the V1 screens are gone).
            "needsV2Onboarding": ob.version != "v2",
            # True only for someone who genuinely used V1 — lets the UI say
            # "welcome back" instead of "welcome" and skip the cold intro.
            "isMigrating": ob.version != "v2" and bool(current.onboardingCompleted),
            # What we already know about them, so the new flow doesn't ask twice.
            "prefill": {
                "phoneNumber": ob.phoneNumber or current.phoneNumber or "",
                "schoolName": ob.schoolName or "",
                "legacyGoal": ob.goal or "",
                "legacyClass": ob.class_ or "",
            },
            # Their work is keyed by user, not by onboarding — it survives.
            "hasCompletedTour": bool(current.hasCompletedTour),
        },
    }


@router.post("/tour-complete")
async def complete_tour(current: User = Depends(get_current_user)) -> dict:
    """The walkthrough was finished (or skipped). Stored on the user so it never
    replays on another device."""
    current.hasCompletedTour = True
    await current.save()
    return {"success": True, "data": {"hasCompletedTour": True}}


@router.post("/exams")
async def set_exams(body: ExamsRequest, current: User = Depends(get_current_user)) -> dict:
    current.onboarding.exams = body.exams
    await current.save()
    return {"success": True, "data": {"exams": current.onboarding.exams}}


@router.post("/profile")
async def set_profile(body: ProfileRequest, current: User = Depends(get_current_user)) -> dict:
    """Phone number + school name. Also mirrored onto the top-level user record,
    which is what the admin panel and success advisors read."""
    phone = body.phoneNumber.strip()
    school = body.schoolName.strip()
    current.onboarding.phoneNumber = phone
    current.onboarding.schoolName = school
    if not current.phoneNumber:
        current.phoneNumber = phone
    await current.save()
    return {"success": True, "data": {"phoneNumber": phone, "schoolName": school}}


@router.post("/research-career")
async def research(
    body: CareerResearchRequest, current: User = Depends(get_current_user)
) -> CareerResearchResponse:
    result = await research_career(body.career, body.exams)

    requirements = [
        SubjectRequirement(
            subject=r.get("subject", ""),
            displayName=r.get("displayName", ""),
            kind=r.get("kind", "compulsory"),
            available=r.get("available", True),
            note=r.get("note"),
        )
        for r in result.get("requirements", [])
        if r.get("subject")
    ]

    # Persist partial progress + research provenance.
    current.onboarding.exams = body.exams
    current.onboarding.career = body.career
    current.onboarding.requiredSubjects = [r.subject for r in requirements if r.kind == "compulsory"]
    current.onboarding.targetScores = result.get("target_scores", {})
    current.onboarding.researchContext = {
        "summary": result.get("summary", ""),
        "sources": result.get("sources", []),
    }
    await current.save()

    return CareerResearchResponse(
        success=True,
        career=body.career,
        summary=result.get("summary", ""),
        requirements=requirements,
        targetScores=result.get("target_scores", {}),
        sources=result.get("sources", []),
    )


@router.post("/subjects")
async def set_subjects(
    body: SubjectsRequest, current: User = Depends(get_current_user)
) -> dict:
    ob = current.onboarding
    ob.exams = body.exams
    ob.career = body.career
    ob.selectedSubjects = normalize_many(body.selectedSubjects)
    if body.requiredSubjects:
        ob.requiredSubjects = normalize_many(body.requiredSubjects)
    if body.targetScores:
        ob.targetScores = body.targetScores
    if body.researchContext:
        ob.researchContext = body.researchContext
    await current.save()
    return {"success": True, "data": {"selectedSubjects": ob.selectedSubjects}}


@router.post("/diagnostic/start")
async def start_diagnostic(
    body: DiagnosticStartRequest, current: User = Depends(get_current_user)
) -> dict:
    subjects = body.subjects or current.onboarding.selectedSubjects
    exams = body.exams or current.onboarding.exams or ["WAEC", "JAMB"]

    questions = await pick_diagnostic_questions(subjects, exams, count=body.count)
    if not questions:
        raise HTTPException(
            status_code=404,
            detail="Could not find diagnostic questions for those subjects yet.",
        )

    items = [
        DiagnosticItem(
            questionId=q.id,
            subject=q.subject,
            exam_name=q.exam_name,
            question_text=q.question_text,
            options=q.options,
            correct_answer=q.correct_answer,  # stored server-side only
        )
        for q in questions
    ]

    session = DiagnosticSession(
        userId=current.id,
        exams=exams,
        career=current.onboarding.career,
        subjects=subjects,
        items=items,
        total=len(items),
        status="in_progress",
    )
    await session.insert()

    # Client payload WITHOUT correct answers.
    client_items = [
        {
            "index": i,
            "subject": it.subject,
            "exam_name": it.exam_name,
            "question_text": it.question_text,
            "options": it.options,
        }
        for i, it in enumerate(items)
    ]
    return {
        "success": True,
        "data": {"sessionId": str(session.id), "items": client_items, "total": len(items)},
    }


@router.post("/diagnostic/submit")
async def submit_diagnostic(
    body: DiagnosticSubmitRequest, current: User = Depends(get_current_user)
) -> dict:
    if not PydanticObjectId.is_valid(body.sessionId):
        raise HTTPException(status_code=400, detail="Invalid session id")
    session = await DiagnosticSession.get(PydanticObjectId(body.sessionId))
    if not session or session.userId != current.id:
        raise HTTPException(status_code=404, detail="Diagnostic session not found")
    if session.status == "completed":
        raise HTTPException(status_code=400, detail="Diagnostic already submitted")

    answers_by_index = {a.index: a.answer for a in body.answers}
    score = 0
    breakdown: dict[str, dict[str, int]] = {}

    for i, item in enumerate(session.items):
        subj = item.subject
        breakdown.setdefault(subj, {"correct": 0, "total": 0})
        breakdown[subj]["total"] += 1
        user_ans = answers_by_index.get(i)
        item.userAnswer = user_ans
        if user_ans is not None and str(user_ans).strip().upper() == str(item.correct_answer).strip().upper():
            item.isCorrect = True
            score += 1
            breakdown[subj]["correct"] += 1
        else:
            item.isCorrect = False

    session.score = score
    session.subjectBreakdown = breakdown
    session.status = "completed"
    session.completedAt = _now()
    await session.save()

    # Finalise onboarding: this is the baseline, not a verdict.
    ob = current.onboarding
    ob.version = "v2"
    current.onboardingCompleted = True
    # store a compact baseline snapshot for the roadmap/probability engine
    current.readiness = current.readiness or {}
    current.readiness["baseline"] = {
        "score": score,
        "total": session.total,
        "subjectBreakdown": breakdown,
        "takenAt": _now().isoformat(),
    }
    await current.save()

    # Record the activity (baseline diagnostic).
    await Activity(
        userId=current.id,
        type="diagnostic",
        status="completed",
        startTime=session.createdAt,
        endTime=_now(),
        metadata={"sessionId": str(session.id), "score": score, "total": session.total},
    ).insert()

    return {
        "success": True,
        "message": "Baseline saved. Your personalised plan is being prepared.",
        "data": {
            "score": score,
            "total": session.total,
            "subjectBreakdown": breakdown,
            "onboardingCompleted": True,
        },
    }
