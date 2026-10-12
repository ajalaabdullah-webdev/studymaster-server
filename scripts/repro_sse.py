"""Reproduce the note-generation SSE against the LIVE server, with timestamps,
to see exactly where/if the stream stalls or ends without completing.
"""

import asyncio
import time

import httpx

from app.core.ids import new_subscription_id, new_user_id
from app.core.security import create_access_token
from app.db import close_db, init_db
from app.models.note import Note
from app.models.quiz import Quiz
from app.models.activity import Activity
from app.models.user import Onboarding, User

TEST_EMAIL = "_e2e_sse@studymaster.test"
BASE = "http://localhost:8000/api/v1"


async def main():
    await init_db()
    await User.find(User.email == TEST_EMAIL).delete()
    user = User(username="SSE E2E", email=TEST_EMAIL, userId=new_user_id(),
                subscriptionId=new_subscription_id(), isEmailVerified=True,
                onboarding=Onboarding(version="v2", exams=["WAEC", "JAMB"]))
    await user.insert()
    token = create_access_token(str(user.id))

    t0 = time.time()
    events = []
    got_complete = False
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(180.0)) as c:
            async with c.stream(
                "POST", f"{BASE}/notes/generate",
                headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
                json={"prompt": "quadratic equations in mathematics for ss3"},
            ) as resp:
                print(f"[{time.time()-t0:5.1f}s] HTTP {resp.status_code}  ct={resp.headers.get('content-type')}")
                buffer = ""
                async for chunk in resp.aiter_text():
                    buffer += chunk
                    buffer = buffer.replace("\r\n", "\n").replace("\r", "\n")
                    while "\n\n" in buffer:
                        part, buffer = buffer.split("\n\n", 1)
                        line = next((l for l in part.split("\n") if l.startswith("data:")), None)
                        if not line:
                            continue
                        import json
                        ev = json.loads(line[5:].strip())
                        events.append(ev)
                        tag = ev.get("type")
                        msg = ev.get("message") or ev.get("title") or (ev.get("note", {}) or {}).get("title", "")
                        print(f"[{time.time()-t0:5.1f}s] {tag:9} {msg}")
                        if tag == "complete":
                            got_complete = True
                        if tag == "error":
                            print("   ERROR payload:", ev)
    except Exception as exc:
        print(f"[{time.time()-t0:5.1f}s] STREAM EXCEPTION: {type(exc).__name__}: {exc}")

    print(f"\nTotal events: {len(events)} | got_complete={got_complete} | elapsed={time.time()-t0:.1f}s")

    # Did a note actually persist?
    note = await Note.find(Note.userId == user.id).sort(-Note.createdAt).first_or_none()
    print("Note persisted:", bool(note), "| quizId:", getattr(note, "quizId", None))

    # cleanup
    await Note.find(Note.userId == user.id).delete()
    await Quiz.find(Quiz.userId == user.id).delete()
    await Activity.find(Activity.userId == user.id).delete()
    await user.delete()
    await close_db()


if __name__ == "__main__":
    asyncio.run(main())
