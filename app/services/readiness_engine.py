"""Gathers readiness signals from the DB, runs the model, smooths, and persists.

Kept separate from ``readiness.py`` (the pure model) so the scoring logic stays
testable and swappable.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from app.models.activity import Activity
from app.models.diagnostic import DiagnosticSession
from app.models.roadmap import Roadmap
from app.models.user import User
from app.services.readiness import (
    PerfSample,
    ReadinessSignals,
    compute_readiness,
    smooth_probability,
)

HISTORY_LIMIT = 90


async def gather_signals(user: User) -> ReadinessSignals:
    now = datetime.now(timezone.utc)
    ob = user.onboarding
    subjects = ob.selectedSubjects or ob.requiredSubjects or []
    exams = ob.exams or ["WAEC", "JAMB"]

    samples: list[PerfSample] = []
    questions_answered = 0

    # Diagnostic baseline (latest completed).
    diag = await DiagnosticSession.find(
        DiagnosticSession.userId == user.id, DiagnosticSession.status == "completed"
    ).sort(-DiagnosticSession.completedAt).first_or_none()
    if diag:
        when = diag.completedAt or diag.createdAt
        for subj, b in (diag.subjectBreakdown or {}).items():
            total = int(b.get("total", 0))
            if total:
                samples.append(PerfSample(subj, int(b.get("correct", 0)), total, when, "diagnostic"))
                questions_answered += total

    # Completed exams (per subject).
    from app.models.exam import Exam  # local import to avoid cycles

    exam_docs = await Exam.find(Exam.userId == user.id, Exam.status == "completed").to_list()
    for e in exam_docs:
        total = int(e.totalQuestions or (len(e.questions) if e.questions else 0))
        if total:
            samples.append(
                PerfSample(str(e.subject).lower(), int(e.score or 0), total, e.createdAt or now, "exam")
            )
            questions_answered += total

    # Completed quizzes (general — no subject).
    from app.models.quiz import Quiz

    quiz_docs = await Quiz.find(Quiz.userId == user.id, Quiz.status == "completed").to_list()
    for q in quiz_docs:
        total = len(q.questions or [])
        if total:
            samples.append(PerfSample(None, int(q.score or 0), total, q.createdAt or now, "quiz"))
            questions_answered += total

    # Consistency: distinct active days in the recent window.
    window_start = now - timedelta(days=14)
    acts = await Activity.find(
        Activity.userId == user.id, Activity.createdAt >= window_start
    ).to_list()
    active_days = {a.createdAt.date() for a in acts if a.createdAt}
    # small volume credit for practice sessions
    questions_answered += sum(5 for a in acts if a.type == "past_question_study")

    # Coverage: roadmap completion fraction.
    roadmap = await Roadmap.find_one(Roadmap.userId == user.id, Roadmap.status == "active")
    done_frac = 0.0
    if roadmap and roadmap.levels:
        total_tasks = sum(len(l.tasks) for l in roadmap.levels)
        done_tasks = sum(1 for l in roadmap.levels for t in l.tasks if t.status == "done")
        done_frac = (done_tasks / total_tasks) if total_tasks else 0.0

    return ReadinessSignals(
        exams=exams,
        subjects=[s.lower() for s in subjects],
        target_scores=ob.targetScores or {},
        samples=samples,
        questions_answered=questions_answered,
        active_days_recent=len(active_days),
        roadmap_done_fraction=done_frac,
        now=now,
    )


def _explain(factors: dict[str, float], subjects: dict[str, float]) -> str:
    weakest = min(subjects.items(), key=lambda kv: kv[1])[0] if subjects else None
    low = min(factors.items(), key=lambda kv: kv[1])[0] if factors else None
    tips = {
        "volume": "practise more past questions",
        "coverage": "work through more of your roadmap",
        "consistency": "study a little every day",
        "breadth": "spread practice across all your subjects",
    }
    parts = []
    if low in tips:
        parts.append(f"To move it up, {tips[low]}")
    if weakest:
        parts.append(f"and focus on {weakest.replace('-', ' ')}")
    return (". ".join(parts) + ".") if parts else "Keep practising consistently to grow this."


async def update_readiness(user: User, *, save: bool = True) -> dict[str, Any]:
    sig = await gather_signals(user)
    result = compute_readiness(sig)

    readiness = dict(user.readiness or {})
    now = sig.now
    today = now.date().isoformat()

    # Baseline for the daily-change clamp = the most recent PRIOR-DAY value, so
    # multiple loads on the same day converge toward the target without letting
    # the probability drift more than one day's worth.
    prior_history = [h for h in readiness.get("history", []) if h.get("date") != today]
    prev_map: dict[str, float] = {}
    for h in reversed(prior_history):
        for exam in result["exams"]:
            if exam not in prev_map and isinstance(h.get(exam), (int, float)):
                prev_map[exam] = float(h[exam])

    exams_out: dict[str, Any] = {}
    history_point: dict[str, Any] = {"date": today}
    for exam, r in result["exams"].items():
        prev = prev_map.get(exam)
        prob = smooth_probability(prev, r["probabilityRaw"])
        exams_out[exam] = {
            "probability": prob,
            "expected": r["expected"],
            "target": r["target"],
            "confidence": r["confidence"],
            "updatedAt": now.isoformat(),
        }
        history_point[exam] = prob

    # Append/replace today's history point; keep the baseline snapshot.
    history = [h for h in readiness.get("history", []) if h.get("date") != today]
    history.append(history_point)
    history = history[-HISTORY_LIMIT:]

    readiness.update(
        {
            "exams": exams_out,
            "subjects": result["subjects"],
            "confidenceFactors": result["confidenceFactors"],
            "overallMastery": result["overallMastery"],
            "explanation": _explain(result["confidenceFactors"], result["subjects"]),
            "history": history,
            "updatedAt": now.isoformat(),
        }
    )
    if "baseline" in (user.readiness or {}):
        readiness["baseline"] = user.readiness["baseline"]

    if save:
        user.readiness = readiness
        await user.save()
    return readiness
