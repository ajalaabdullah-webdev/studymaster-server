"""Phase 3 readiness tests: pure model + end-to-end (throwaway user)."""

import asyncio
from datetime import datetime, timedelta, timezone

import httpx

from app.core.ids import new_subscription_id, new_user_id
from app.core.security import create_access_token
from app.db import close_db, init_db
from app.main import app
from app.models.activity import Activity
from app.models.diagnostic import DiagnosticItem, DiagnosticSession
from app.models.exam import Exam, ExamQuestion
from app.models.user import Onboarding, User
from app.services.readiness import (
    PerfSample, ReadinessSignals, compute_readiness, smooth_probability,
)

TEST_EMAIL = "_e2e_readiness@studymaster.test"


def unit_tests():
    now = datetime.now(timezone.utc)
    subjects = ["mathematics", "english-language", "physics"]

    # Beginner: only a weak baseline, no coverage/consistency.
    beginner = ReadinessSignals(
        exams=["JAMB"], subjects=subjects, target_scores={"JAMB": 250},
        samples=[PerfSample("mathematics", 1, 4, now, "diagnostic"),
                 PerfSample("physics", 2, 6, now, "diagnostic")],
        questions_answered=10, active_days_recent=1, roadmap_done_fraction=0.0, now=now,
    )
    b = compute_readiness(beginner)
    print("beginner:", b["exams"]["JAMB"], "| conf:", b["confidenceFactors"])

    # Strong + consistent + good coverage, lots of recent high-accuracy practice.
    strong_samples = [PerfSample(s, 17, 20, now, "exam") for s in subjects] * 3
    strong = ReadinessSignals(
        exams=["JAMB"], subjects=subjects, target_scores={"JAMB": 250},
        samples=strong_samples, questions_answered=400, active_days_recent=12,
        roadmap_done_fraction=0.7, now=now,
    )
    s = compute_readiness(strong)
    print("strong:  ", s["exams"]["JAMB"], "| conf:", s["confidenceFactors"])

    assert b["exams"]["JAMB"]["probabilityRaw"] < s["exams"]["JAMB"]["probabilityRaw"], \
        "strong student must have higher probability than beginner"
    assert b["confidenceFactors"]["consistency"] < s["confidenceFactors"]["consistency"]

    # Smoothing: conservative first read, clamped daily movement.
    first = smooth_probability(None, 0.62)
    assert first <= 0.15, f"first read should start low, got {first}"
    moved = smooth_probability(0.10, 0.95)
    assert moved <= 0.10 + 0.061, f"daily jump not clamped: {moved}"
    down = smooth_probability(0.50, 0.0)
    assert down >= 0.50 - 0.061, f"daily drop not clamped: {down}"
    print("smoothing: first=", first, "clamped_up=", moved, "clamped_down=", down)
    print("UNIT TESTS PASSED ✅\n")


async def _cleanup(uid):
    await DiagnosticSession.find(DiagnosticSession.userId == uid).delete()
    await Exam.find(Exam.userId == uid).delete()
    await Activity.find(Activity.userId == uid).delete()
    u = await User.get(uid)
    if u:
        await u.delete()


async def e2e():
    await init_db()
    await User.find(User.email == TEST_EMAIL).delete()
    now = datetime.now(timezone.utc)

    user = User(
        username="Readiness E2E", email=TEST_EMAIL,
        userId=new_user_id(), subscriptionId=new_subscription_id(), isEmailVerified=True,
        onboardingCompleted=True,
        onboarding=Onboarding(version="v2", exams=["JAMB", "WAEC"],
                              career="engineering",
                              selectedSubjects=["mathematics", "english-language", "physics"],
                              targetScores={"JAMB": 250}),
    )
    await user.insert()

    # a completed diagnostic + an exam + some activity
    await DiagnosticSession(
        userId=user.id, status="completed", score=5, total=10, completedAt=now,
        subjectBreakdown={"mathematics": {"correct": 2, "total": 4},
                          "physics": {"correct": 3, "total": 6}},
        items=[DiagnosticItem(subject="mathematics", exam_name="JAMB",
                              question_text="q", correct_answer="A")],
    ).insert()
    await Exam(userId=user.id, subject="mathematics", exam_name="JAMB",
               question_type="obj", score=14, totalQuestions=20, status="completed",
               questions=[ExamQuestion(isCorrect=True)]).insert()
    for i in range(6):
        await Activity(userId=user.id, type="past_question_study", status="completed",
                       startTime=now - timedelta(days=i), endTime=now - timedelta(days=i),
                       createdAt=now - timedelta(days=i)).insert()

    token = create_access_token(str(user.id))
    headers = {"Authorization": f"Bearer {token}"}
    transport = httpx.ASGITransport(app=app)
    try:
        async with httpx.AsyncClient(transport=transport, base_url="http://test", timeout=60) as c:
            r = await c.get("/api/v1/readiness", headers=headers)
            print("GET /readiness ->", r.status_code)
            d = r.json()["data"]
            print("   exams:", d["exams"])
            print("   subjects:", d["subjects"])
            print("   factors:", d["confidenceFactors"])
            print("   explanation:", d["explanation"])
            assert r.status_code == 200
            jamb = d["exams"]["JAMB"]["probability"]
            assert 0.02 <= jamb <= 0.97
            assert len(d["history"]) == 1 and d["history"][0]["date"] == now.date().isoformat()
            assert set(d["subjects"].keys()) == {"mathematics", "english-language", "physics"}

            # second same-day call must not jump.
            r2 = await c.get("/api/v1/readiness", headers=headers)
            jamb2 = r2.json()["data"]["exams"]["JAMB"]["probability"]
            assert abs(jamb2 - jamb) <= 0.061, f"same-day jump: {jamb} -> {jamb2}"
            print(f"   stable same-day: {jamb} -> {jamb2}")
        print("\nREADINESS E2E TEST PASSED ✅")
    finally:
        await _cleanup(user.id)
        print("cleaned up")
        await close_db()


async def main():
    unit_tests()
    await e2e()


if __name__ == "__main__":
    asyncio.run(main())
