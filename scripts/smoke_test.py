"""Phase 0 smoke test: init Beanie against the real DB and load real docs
through the Pydantic models to catch schema mismatches early.

Run: PYTHONPATH=. .venv/bin/python scripts/smoke_test.py
"""

import asyncio

from app.db import close_db, init_db
from app.models import Activity, DiagnosticSession, Question, User


async def main() -> None:
    await init_db()
    print("Beanie initialised OK")

    # Load a batch of real users through the User model.
    users = await User.find_all(limit=60).to_list()
    print(f"Loaded {len(users)} users through the model")
    with_onboarding = sum(1 for u in users if u.onboarding and u.onboarding.class_)
    completed = sum(1 for u in users if u.onboardingCompleted)
    print(f"  with legacy onboarding.class: {with_onboarding}, onboardingCompleted: {completed}")
    if users:
        u = users[0]
        pub = u.public_dict()
        assert "password" not in pub, "password leaked into public_dict!"
        print(f"  sample public_dict keys: {sorted(pub.keys())[:8]} ...")

    # Load questions of every type (obj/theory/practical) to validate the enum fix.
    for qt in ("obj", "theory", "practical"):
        q = await Question.find(Question.question_type == qt).first_or_none()
        print(f"  question_type={qt}: {'OK' if q else 'none found'}"
              + (f" (subject={q.subject}, exam={q.exam_name})" if q else ""))

    # WAEC+JAMB only count (what students will see).
    waec_jamb = await Question.find(
        {"exam_name": {"$in": ["WAEC", "JAMB"]}}
    ).count()
    print(f"  WAEC+JAMB questions: {waec_jamb}")

    # Load activities (probability-model signal).
    acts = await Activity.find_all(limit=5).to_list()
    print(f"Loaded {len(acts)} activities through the model")

    print("\nSMOKE TEST PASSED ✅")
    await close_db()


if __name__ == "__main__":
    asyncio.run(main())
