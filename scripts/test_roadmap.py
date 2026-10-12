"""Phase 2 roadmap end-to-end test (throwaway user, cleaned up)."""

import asyncio

import httpx

from app.core.ids import new_subscription_id, new_user_id
from app.core.security import create_access_token
from app.db import close_db, init_db
from app.main import app
from app.models.activity import Activity
from app.models.roadmap import Roadmap
from app.models.user import Onboarding, User

TEST_EMAIL = "_e2e_roadmap@studymaster.test"


async def _cleanup(uid):
    await Roadmap.find(Roadmap.userId == uid).delete()
    await Activity.find(Activity.userId == uid).delete()
    u = await User.get(uid)
    if u:
        await u.delete()


async def main():
    await init_db()
    await User.find(User.email == TEST_EMAIL).delete()

    user = User(
        username="Roadmap E2E",
        email=TEST_EMAIL,
        userId=new_user_id(),
        subscriptionId=new_subscription_id(),
        isEmailVerified=True,
        onboardingCompleted=True,
        onboarding=Onboarding(
            version="v2",
            exams=["WAEC", "JAMB"],
            career="software engineering",
            selectedSubjects=["mathematics", "english-language", "physics"],
            requiredSubjects=["mathematics", "english-language"],
            targetScores={"JAMB": 250},
        ),
        readiness={"baseline": {"score": 4, "total": 10,
                                "subjectBreakdown": {"mathematics": {"correct": 1, "total": 4},
                                                     "physics": {"correct": 3, "total": 6}}}},
    )
    await user.insert()
    token = create_access_token(str(user.id))
    headers = {"Authorization": f"Bearer {token}"}

    transport = httpx.ASGITransport(app=app)
    try:
        async with httpx.AsyncClient(transport=transport, base_url="http://test", timeout=180) as c:
            print("generating roadmap (live exam-date search + LLM plan)...")
            r = await c.post("/api/v1/roadmap/generate", headers=headers)
            print("generate ->", r.status_code)
            body = r.json()
            assert r.status_code == 200, body
            rm = body["data"]
            print("   summary:", (rm["summary"] or "")[:160])
            print("   horizonWeeks:", rm["horizonWeeks"], "| examDate:", rm["examDate"])
            print("   phases:", [p["title"] for p in rm["phases"]])
            print("   #levels:", len(rm["levels"]))
            assert len(rm["levels"]) > 0
            l1 = rm["levels"][0]
            print("   level1:", l1["title"], "| status:", l1["status"], "| tasks:", len(l1["tasks"]))
            assert l1["status"] == "available"
            assert all(t["id"] for t in l1["tasks"])
            # subjects on tasks are valid slugs or null
            valid = set(rm["subjects"])
            assert all((t["subject"] in valid or t["subject"] is None) for lvl in rm["levels"] for t in lvl["tasks"])
            # a mock exam appears somewhere in the last 3 levels
            last3 = rm["levels"][-3:]
            has_exam = any(t["type"] == "exam" for lvl in last3 for t in lvl["tasks"])
            print("   mock exam in final weeks:", has_exam)

            # current week
            r = await c.get("/api/v1/roadmap/current", headers=headers)
            cur = r.json()["data"]
            print("current ->", r.status_code, "progress:", cur["progress"])
            assert cur["level"]["order"] == 1

            # complete all level-1 tasks -> level done, level 2 unlocked
            for t in l1["tasks"]:
                r = await c.post(f"/api/v1/roadmap/task/{t['id']}/complete", headers=headers)
                assert r.status_code == 200, r.json()
            r = await c.get("/api/v1/roadmap", headers=headers)
            rm2 = r.json()["data"]
            assert rm2["levels"][0]["status"] == "done", "level 1 should be done"
            if len(rm2["levels"]) > 1:
                assert rm2["levels"][1]["status"] == "available", "level 2 should unlock"
            print("   level1 done, level2 unlocked ✅")

            # idempotency of active roadmap (GET should not regenerate)
            v1 = rm2["version"]
            r = await c.get("/api/v1/roadmap", headers=headers)
            assert r.json()["data"]["version"] == v1, "GET must not regenerate"

        print("\nROADMAP E2E TEST PASSED ✅")
    finally:
        await _cleanup(user.id)
        print("cleaned up")
        await close_db()


if __name__ == "__main__":
    asyncio.run(main())
