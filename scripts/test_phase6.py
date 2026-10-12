"""Phase 6 smoke test — every ported endpoint against the live server (test user)."""

import asyncio

import httpx

from app.core.security import create_access_token
from app.db import close_db, init_db
from app.models.user import User

BASE = "http://localhost:8000/api/v1"


async def main():
    await init_db()
    user = await User.find_one(User.email == "_e2e_browser@studymaster.test")
    assert user, "test user missing"
    tok = create_access_token(str(user.id))
    H = {"Authorization": f"Bearer {tok}"}

    async with httpx.AsyncClient(timeout=120) as c:
        # questions (exam-first filters)
        r = await c.get(f"{BASE}/questions/filters?exam_name=JAMB")
        d = r.json()["data"]
        print(f"filters(JAMB): {r.status_code} subjects={len(d['subjects'])} years={len(d['years'])} exams={d['examNames']}")
        r = await c.get(f"{BASE}/questions?exam_name=JAMB&subject=physics&limit=5")
        print(f"questions list: {r.status_code} count={r.json()['count']} total={r.json()['pagination']['total']}")

        # exam: generate small + submit objective
        r = await c.post(f"{BASE}/exams/generate", headers=H,
                         json={"subject": "physics", "exam_name": "JAMB", "question_type": "obj", "size": 3})
        exam = r.json()["data"]
        qs = exam["questions"]
        print(f"exam generate: {r.status_code} questions={len(qs)} populated={bool(qs[0]['questionId'].get('question_text'))}")
        answers = [{"questionId": q["questionId"]["_id"], "userAnswer": "A"} for q in qs]
        r = await c.post(f"{BASE}/exams/{exam['_id']}/submit-objective", headers=H, json={"answers": answers})
        sub = r.json()["data"]
        fb = sum(1 for q in sub["questions"] if q.get("aiFeedback"))
        print(f"exam submit-obj: {r.status_code} score={sub['score']}/{sub['totalQuestions']} aiFeedback_on={fb}")
        r = await c.get(f"{BASE}/exams/history", headers=H)
        print(f"exam history: {r.status_code} count={r.json()['count']}")

        # coach chat
        r = await c.post(f"{BASE}/chats", headers=H, json={})
        chat_id = r.json()["data"]["_id"]
        r = await c.post(f"{BASE}/chats/{chat_id}/messages", headers=H,
                         json={"message": "In one sentence, what is osmosis?"})
        print(f"coach message: {r.status_code} reply='{r.json()['assistantResponse'][:60]}...'")

        # dashboard (readiness + trend + motivation)
        r = await c.get(f"{BASE}/dashboard", headers=H)
        d = r.json()["data"]
        print(f"dashboard: {r.status_code} stats={d['overallStats']['examsCompleted']} exams, "
              f"readiness_keys={sorted(d['readiness'].get('exams', {}).keys())}, trend_days={len(d['studyTrend'])}")

        # community
        r = await c.get(f"{BASE}/community/posts")
        print(f"community posts: {r.status_code} count={r.json()['count']}")

        # quizzes library
        r = await c.get(f"{BASE}/quizzes", headers=H)
        print(f"quizzes: {r.status_code} count={r.json()['count']}")

        # game leaderboard
        r = await c.get(f"{BASE}/game/leaderboard", headers=H)
        print(f"leaderboard: {r.status_code} rows={len(r.json()['data'])}")

    print("\nPHASE 6 BACKEND SMOKE PASSED ✅")
    await close_db()


if __name__ == "__main__":
    asyncio.run(main())
