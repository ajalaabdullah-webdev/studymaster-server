"""Phase 4.1 test: intent routing, human summaries, edit-in-place, read-along."""

import asyncio
import json

import httpx

from app.agents.note_agent import classify_intent
from app.core.ids import new_subscription_id, new_user_id
from app.core.security import create_access_token
from app.db import close_db, init_db
from app.main import app
from app.models.activity import Activity
from app.models.note import Note
from app.models.quiz import Quiz
from app.models.user import Onboarding, User
from app.services.note_speech import segment_note

TEST_EMAIL = "_e2e_notes2@studymaster.test"


async def collect(client, headers, body):
    events = []
    async with client.stream("POST", "/api/v1/notes/assist", headers=headers, json=body) as r:
        buffer = ""
        async for chunk in r.aiter_text():
            buffer = (buffer + chunk).replace("\r\n", "\n")
            while "\n\n" in buffer:
                part, buffer = buffer.split("\n\n", 1)
                line = next((l for l in part.split("\n") if l.startswith("data:")), None)
                if line:
                    events.append(json.loads(line[5:].strip()))
    return events


async def main():
    await init_db()
    await User.find(User.email == TEST_EMAIL).delete()
    user = User(username="Notes2", email=TEST_EMAIL, userId=new_user_id(),
                subscriptionId=new_subscription_id(), isEmailVerified=True,
                onboarding=Onboarding(version="v2", exams=["WAEC"], selectedSubjects=["biology"]))
    await user.insert()
    token = create_access_token(str(user.id))
    headers = {"Authorization": f"Bearer {token}"}

    # ---- intent classification ----
    i1 = await classify_intent("make a note on osmosis", note_open=False)
    i2 = await classify_intent("add more images and make it simpler", note_open=True, note_title="Osmosis")
    i3 = await classify_intent("what does osmosis actually mean?", note_open=True, note_title="Osmosis")
    print("intents:", i1, "|", i2, "|", i3)
    assert i1 == "generate" and i2 == "edit" and i3 == "answer", f"intent mismatch: {i1},{i2},{i3}"

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test", timeout=180) as c:
        # ---- GENERATE via assist ----
        print("\n[assist] generating…")
        evs = await collect(c, headers, {"prompt": "create a note on osmosis in biology"})
        types = [e["type"] for e in evs]
        complete = next((e for e in evs if e["type"] == "complete"), None)
        msg = next((e["message"] for e in evs if e["type"] == "assistant_message"), None)
        print("  event types:", types)
        print("  summary:", msg)
        assert evs[0]["type"] == "intent" and evs[0]["intent"] == "generate"
        assert complete, "no complete event"
        assert msg and "your note is ready" not in msg.lower(), "summary should be human, not canned"
        note_id = complete["note"]["noteId"]
        note = await Note.get(Note.id.__class__(note_id))
        note_count = await Note.find(Note.userId == user.id).count()
        img_n = len(complete["note"].get("images", []))
        print(f"  note_id={note_id}  images={img_n}  notes_in_db={note_count}")
        assert note_count == 1

        # ---- EDIT via assist (must NOT create a new note) ----
        print("\n[assist] editing (should edit in place)…")
        evs = await collect(c, headers, {"prompt": "please add an example and make the grammar simpler",
                                          "noteId": note_id})
        assert evs[0]["intent"] == "edit", f"expected edit, got {evs[0]}"
        note_count2 = await Note.find(Note.userId == user.id).count()
        print("  notes_in_db after edit:", note_count2, "(must stay 1)")
        assert note_count2 == 1, "edit created a duplicate note!"

        # ---- ANSWER via assist (question, no note change) ----
        print("\n[assist] answering a question…")
        evs = await collect(c, headers, {"prompt": "what is the main idea here in one line?", "noteId": note_id})
        assert evs[0]["intent"] == "answer", f"expected answer, got {evs[0]}"
        ans = next((e["message"] for e in evs if e["type"] == "answer"), None)
        print("  answer:", (ans or "")[:120])
        assert ans
        assert await Note.find(Note.userId == user.id).count() == 1, "answering must not create a note"

        # ---- per-note chat persisted ----
        fresh = await Note.get(Note.id.__class__(note_id))
        print("  chatHistory turns:", len(fresh.chatHistory))
        assert len(fresh.chatHistory) >= 6  # gen(2)+edit(2)+answer(2)

        # ---- READ-ALONG segmentation + audio ----
        segs = segment_note(fresh.content)
        print(f"\n[read-along] {len(segs)} segments; sample:", segs[0]["spoken"][:70] if segs else "none")
        print("  building audio (Spitch + Cloudinary)…")
        r = await c.post(f"/api/v1/notes/{note_id}/read-along", headers=headers)
        data = r.json()["data"]
        with_audio = sum(1 for s in data["segments"] if s.get("audioUrl"))
        print(f"  segments={len(data['segments'])}  with_audio={with_audio}")
        assert r.status_code == 200 and len(data["segments"]) == len(segs)
        assert with_audio >= max(1, len(segs) - 2), "most segments should have audio"

    print("\nNOTES 4.1 TEST PASSED ✅")
    await Note.find(Note.userId == user.id).delete()
    await Quiz.find(Quiz.userId == user.id).delete()
    await Activity.find(Activity.userId == user.id).delete()
    await user.delete()
    await close_db()


if __name__ == "__main__":
    asyncio.run(main())
