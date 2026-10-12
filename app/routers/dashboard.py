"""Dashboard endpoint — ported from Node, upgraded with readiness + roadmap focus.

    GET /api/v1/dashboard
"""

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends

from app.core.deps import get_current_user
from app.core.llm import chat
from app.models.activity import Activity
from app.models.exam import Exam
from app.models.note import Note
from app.models.quiz import Quiz
from app.models.roadmap import Roadmap
from app.models.user import User
from app.services.readiness_engine import update_readiness

router = APIRouter(prefix="/api/v1/dashboard", tags=["dashboard"])


@router.get("")
async def get_dashboard(current: User = Depends(get_current_user)) -> dict:
    now = datetime.now(timezone.utc)

    recent = await Activity.find(
        {"userId": current.id, "type": {"$ne": "app_session"}}  # study activities only
    ).sort(-Activity.createdAt).limit(5).to_list()

    # totals
    total_secs = 0
    async for a in Activity.find(Activity.userId == current.id):
        total_secs += a.duration or 0
    study_hours = f"{max(total_secs / 3600, 0.1):.1f}" if total_secs else "0.0"

    notes_generated = await Note.find(Note.userId == current.id).count()
    quizzes_completed = await Quiz.find(Quiz.userId == current.id, Quiz.status == "completed").count()
    pq_practised = await Activity.find(
        Activity.userId == current.id, Activity.type == "past_question_study", Activity.status == "completed"
    ).count()

    # 7-day study trend
    seven_ago = (now - timedelta(days=6)).replace(hour=0, minute=0, second=0, microsecond=0)
    per_day: dict[str, float] = {}
    async for a in Activity.find(Activity.userId == current.id, Activity.createdAt >= seven_ago):
        if a.createdAt:
            key = a.createdAt.date().isoformat()
            per_day[key] = per_day.get(key, 0) + (a.duration or 0)
    trend = []
    for i in range(6, -1, -1):
        d = (now - timedelta(days=i)).date()
        trend.append({"day": d.strftime("%a"), "hours": round(per_day.get(d.isoformat(), 0) / 3600, 1)})

    # performance across exams + quizzes
    exams = await Exam.find(Exam.userId == current.id, Exam.status == "completed").to_list()
    quizzes = await Quiz.find(Quiz.userId == current.id, Quiz.status == "completed").to_list()
    subj: dict[str, dict[str, float]] = {}
    total_score = total_qs = 0
    for e in exams:
        s = subj.setdefault(e.subject.lower(), {"score": 0, "total": 0})
        s["score"] += e.score or 0
        s["total"] += e.totalQuestions or 0
        total_score += e.score or 0
        total_qs += e.totalQuestions or 0
    for q in quizzes:
        s = subj.setdefault("note quizzes", {"score": 0, "total": 0})
        s["score"] += q.score or 0
        s["total"] += len(q.questions or [])
        total_score += q.score or 0
        total_qs += len(q.questions or [])

    subject_stats = {k: round(v["score"] / v["total"] * 100, 1) for k, v in subj.items() if v["total"]}
    strong = max(subject_stats, key=subject_stats.get) if subject_stats else None
    weak = min(subject_stats, key=subject_stats.get) if subject_stats else None
    overall = f"{(total_score / total_qs * 100):.2f}" if total_qs else "0.00"

    # readiness + today's roadmap focus (the "teacher" layer)
    readiness = await update_readiness(current)
    roadmap = await Roadmap.find_one(Roadmap.userId == current.id, Roadmap.status == "active")
    todays_tasks = []
    if roadmap:
        for level in roadmap.levels:
            if level.status in {"available", "in_progress"}:
                todays_tasks = [t.model_dump(mode="json") for t in level.tasks if t.status != "done"][:3]
                break

    # motivation (short, personal)
    try:
        parts = [f"Student: {current.username}, preparing for {', '.join(current.onboarding.exams or []) or 'WAEC/JAMB'}."]
        if weak:
            parts.append(f"Could use encouragement in {weak}.")
        parts.append("Recently active." if recent else "Hasn't been active lately — give a gentle nudge.")
        motivation = await chat(
            [{"role": "system", "content": "You are an inspiring, warm Nigerian mentor. Max 2 sentences, plain text."},
             {"role": "user", "content": " ".join(parts)}],
            tier="default", temperature=0.8, max_tokens=90,
        )
    except Exception:  # noqa: BLE001
        motivation = "Every day you show up, you get closer to your goal. Keep going!"

    return {
        "success": True,
        "data": {
            "user": {"username": current.username, "class": current.onboarding.class_ or "student"},
            "recentActivity": [
                {**a.model_dump(mode="json"), "_id": str(a.id)} for a in recent
            ],
            "overallStats": {
                "examsCompleted": len(exams),
                "totalQuestionsPracticed": total_qs,
                "notesGenerated": notes_generated,
                "studyHours": study_hours,
                "quizzesCompleted": quizzes_completed,
                "pastQuestionsPracticed": pq_practised,
            },
            "studyTrend": trend,
            "performance": {
                "overallAverage": overall,
                "strongSubject": strong,
                "weakSubject": weak,
                "subjectStats": subject_stats,
            },
            "readiness": readiness,
            "todaysTasks": todays_tasks,
            "motivation": motivation,
        },
    }
