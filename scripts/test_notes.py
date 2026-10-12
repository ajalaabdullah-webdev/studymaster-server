"""Phase 4 note-agent test (throwaway user, cleaned up).

Exercises the agentic pipeline against live LLM + Tavily + Wikimedia, then a real
Spitch TTS check. Nothing touches real users.
"""

import asyncio

from app.core.ids import new_subscription_id, new_user_id
from app.db import close_db, init_db
from app.models.activity import Activity
from app.models.note import Note
from app.models.quiz import Quiz
from app.models.user import Onboarding, User
from app.agents.note_agent import generate_note
from app.services import spitch, wikimedia

TEST_EMAIL = "_e2e_notes@studymaster.test"


async def _cleanup(uid):
    await Note.find(Note.userId == uid).delete()
    await Quiz.find(Quiz.userId == uid).delete()
    await Activity.find(Activity.userId == uid).delete()
    u = await User.get(uid)
    if u:
        await u.delete()


async def main():
    await init_db()
    await User.find(User.email == TEST_EMAIL).delete()
    user = User(username="Notes E2E", email=TEST_EMAIL,
                userId=new_user_id(), subscriptionId=new_subscription_id(),
                isEmailVerified=True,
                onboarding=Onboarding(version="v2", exams=["WAEC", "JAMB"]))
    await user.insert()

    # --- quick Wikimedia sanity ---
    imgs = await wikimedia.search_images("human heart anatomy diagram", limit=3)
    print(f"wikimedia: {len(imgs)} images; first: {imgs[0]['title'][:50] if imgs else 'none'}")

    events = []
    async def emit(ev):
        if ev["type"] == "progress":
            print("   …", ev["message"])
        events.append(ev)

    try:
        print("generating note (live LLM + search + images)...")
        result = await generate_note(
            user_id=user.id, prompt="Photosynthesis in plants",
            onboarding={"class": "SS2"}, emit=emit,
        )
        print("note title:", result["title"])
        print("subject:", result["subject"], "| topic:", result["topic"])
        print("content length:", len(result["content"]))
        print("images embedded:", len(result["images"]))
        print("quiz questions:", len(result["quiz"]))
        has_html = "<h" in result["content"] and "<p" in result["content"]
        has_figure = "<figure" in result["content"]
        print("has html headings:", has_html, "| has <figure>:", has_figure)

        assert result["noteId"]
        assert has_html, "note should be HTML"
        assert len(result["quiz"]) >= 4, "expected a quiz"
        # progress events streamed
        steps = [e.get("step") for e in events if e["type"] == "progress"]
        print("progress steps:", steps)
        assert "write" in steps and "quiz" in steps

        # verify saved
        note = await Note.get(user.id.__class__(result["noteId"]))
        assert note and note.quizId, "note+quiz should be saved with link"

        # --- Spitch TTS check (real) ---
        speech_text = spitch.html_to_speech_text(result["content"])
        print("speech text preview:", speech_text[:80])
        if spitch.available():
            audio = await spitch.text_to_speech("Hello, this is StudyMaster reading your note.")
            print("spitch TTS bytes:", len(audio))
            assert len(audio) > 500, "expected audio bytes"
        else:
            print("spitch not configured — skipped TTS")

        print("\nNOTES E2E TEST PASSED ✅")
    finally:
        await _cleanup(user.id)
        print("cleaned up")
        await close_db()


if __name__ == "__main__":
    asyncio.run(main())
