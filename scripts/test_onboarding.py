"""Phase 1 onboarding end-to-end test.

Creates a THROWAWAY user, runs the V2 onboarding flow against the real question
bank + real research agent, asserts results, then deletes everything it created.
No real user is touched.

Run: PYTHONPATH=. .venv/bin/python scripts/test_onboarding.py
"""

import asyncio

import httpx

from app.core.ids import new_subscription_id, new_user_id
from app.core.security import create_access_token
from app.db import close_db, init_db
from app.main import app
from app.models.activity import Activity
from app.models.diagnostic import DiagnosticSession
from app.models.user import User

TEST_EMAIL = "_e2e_onboarding@studymaster.test"


async def _cleanup(user_id) -> None:
    await DiagnosticSession.find(DiagnosticSession.userId == user_id).delete()
    await Activity.find(Activity.userId == user_id).delete()
    u = await User.get(user_id)
    if u:
        await u.delete()


async def main() -> None:
    await init_db()

    # Fresh throwaway user
    await User.find(User.email == TEST_EMAIL).delete()
    user = User(
        username="E2E Test",
        email=TEST_EMAIL,
        userId=new_user_id(),
        subscriptionId=new_subscription_id(),
        isEmailVerified=True,
    )
    await user.insert()
    token = create_access_token(str(user.id))
    headers = {"Authorization": f"Bearer {token}"}

    transport = httpx.ASGITransport(app=app)
    try:
        async with httpx.AsyncClient(transport=transport, base_url="http://test", timeout=90) as c:
            # 1. exams
            r = await c.post("/api/v1/onboarding/exams", headers=headers,
                             json={"exams": ["WAEC", "JAMB"]})
            print("exams ->", r.status_code, r.json().get("data"))
            assert r.status_code == 200

            # 2. research-career (REAL Tavily + LLM call)
            print("researching career (live search + LLM)...")
            r = await c.post("/api/v1/onboarding/research-career", headers=headers,
                             json={"career": "software engineering", "exams": ["WAEC", "JAMB"]})
            print("research-career ->", r.status_code)
            body = r.json()
            assert r.status_code == 200, body
            subs = [x["subject"] for x in body["requirements"]]
            print("   summary:", (body.get("summary") or "")[:160])
            print("   subjects:", subs)
            print("   targetScores:", body.get("targetScores"))
            print("   sources:", len(body.get("sources", [])))
            assert any("english" in s for s in subs), "expected English in requirements"

            # 3. subjects
            chosen = ["mathematics", "english language", "physics"]
            r = await c.post("/api/v1/onboarding/subjects", headers=headers,
                             json={"exams": ["WAEC", "JAMB"], "career": "software engineering",
                                   "selectedSubjects": chosen})
            print("subjects ->", r.status_code, r.json().get("data"))
            assert r.status_code == 200

            # 4. diagnostic start (real question bank)
            r = await c.post("/api/v1/onboarding/diagnostic/start", headers=headers,
                             json={"subjects": chosen, "exams": ["WAEC", "JAMB"], "count": 10})
            print("diagnostic/start ->", r.status_code)
            data = r.json()["data"]
            items = data["items"]
            print(f"   got {len(items)} items; subjects seen:",
                  sorted({it['subject'] for it in items}))
            assert len(items) > 0
            # Ensure NO correct answers leaked to the client.
            assert all("correct_answer" not in it for it in items), "correct answer leaked!"
            assert all(it.get("options") for it in items), "an item had no options"

            # 5. diagnostic submit (answer everything 'A')
            answers = [{"index": it["index"], "answer": "A"} for it in items]
            r = await c.post("/api/v1/onboarding/diagnostic/submit", headers=headers,
                             json={"sessionId": data["sessionId"], "answers": answers})
            print("diagnostic/submit ->", r.status_code)
            sub = r.json()["data"]
            print(f"   score {sub['score']}/{sub['total']}, breakdown keys:",
                  list(sub["subjectBreakdown"].keys()))
            assert r.status_code == 200 and sub["onboardingCompleted"] is True

            # 6. status reflects completion
            r = await c.get("/api/v1/onboarding/status", headers=headers)
            st = r.json()["data"]
            print("status ->", {k: st[k] for k in ("onboardingCompleted", "version", "needsV2Onboarding")})
            assert st["onboardingCompleted"] and st["version"] == "v2"
            assert st["needsV2Onboarding"] is False

            # verify baseline persisted on the user
            fresh = await User.get(user.id)
            assert fresh.readiness.get("baseline"), "baseline not saved on user"
            acts = await Activity.find(Activity.userId == user.id, Activity.type == "diagnostic").to_list()
            assert len(acts) == 1, "diagnostic activity not recorded"

        print("\nONBOARDING E2E TEST PASSED ✅")
    finally:
        await _cleanup(user.id)
        print("cleaned up throwaway user + sessions")
        await close_db()


if __name__ == "__main__":
    asyncio.run(main())
