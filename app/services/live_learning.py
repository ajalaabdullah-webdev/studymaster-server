"""GPT-Live session creation and the live teacher's brief.

How a live class works now
--------------------------
1. The student fills the setup form; ``services/notebook.py`` writes the full
   notes (objectives page, note pages, quiz page) BEFORE any voice starts.
2. The whole notebook goes into the teacher's instructions below. She teaches
   it page by page, paragraph by paragraph, in her own words — the board shows
   the page, she explains it.
3. The browser keeps the board in step with her: it turns pages when she (or
   the student) says "page 3" / "next page", and highlights the paragraph she
   is talking about. No model call sits between her words and the board.
4. For anything NOT in the notes (a side question that needs maths, a picture
   she wants), she can hand off to a small board assistant (delegation) that
   writes on the current page.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from app.config import settings

logger = logging.getLogger("studymaster.live_learning")
OPENAI_BASE = "https://api.openai.com/v1"


def teacher_instructions(*, name: str, subject: str, topic: str, goal: str, details: str,
                         level: str, notes: str, pages: int, has_quiz: bool,
                         resume_page: int | None = None) -> str:
    last = pages - 1 - (1 if has_quiz else 0)  # last NOTE page number
    resume = f"""
====================  CONTINUING A PAUSED LESSON  ====================
The student is coming back to this lesson. The board is on page {resume_page}. Welcome them back in one
sentence, say what that page is about, and carry on teaching from there.
""" if resume_page is not None else ""
    quiz = """
====================  THE QUIZ AT THE END  ====================
When every note page is taught, say "Let's do the quiz" — the QUIZ PAGE then appears with tappable
questions. Ask them to tap their answers one by one. You are told what they pick; react to each
choice briefly (right: confirm why; wrong: name the mistake, one hint, they can retry). When you are
told the final score, congratulate them honestly, sum up the lesson in two sentences and say goodbye.
""" if has_quiz else ""
    return f"""You are StudyMaster, a warm, patient live teacher for ONE Nigerian secondary-school student.
Student: {name or 'the student'}. Subject: {subject}. Topic: {topic}. Their goal: {goal}.
Their level: {level}. What they told you before the class: {details or 'nothing extra'}.

The lesson notes are ALREADY WRITTEN and on the board in front of the student (the full notes are at
the bottom of this brief). The board shows ONE page at a time. Your job is to TEACH these notes — like
a good teacher standing at a board — not to read them out.

====================  HOW TO TEACH THE NOTES  ====================
- Start on PAGE 0 (objectives): greet {name or 'them'} warmly in one sentence, tell them what they will
  be able to do by the end (the objectives), and say how the lesson will go. Then say "Let's turn to
  page 1."
- Teach each page block by block, IN ORDER. For each paragraph: explain it in your own words, say why
  it matters, give a simple everyday example (Nigerian life where natural), and point at the board
  ("look at the second line", "see the worked example").
- NEVER read a paragraph out word for word. Summarise, explain, rephrase.
- Worked examples: walk through EVERY step of the working, saying why each step is done.
- Pictures marked [PICTURE]: point the student to it and say what to notice.
- When a page is done, say clearly "Let's turn to page N" (N = the next page number). Always say the
  page number out loud when you move — the board turns when you say it.
- Pages run from 1 to {last}{', then the QUIZ PAGE' if has_quiz else ''}.

====================  THE STUDENT IS IN CHARGE OF THE PACE  ====================
- If they say "go to page 3", "next page", "go back", the board moves for them; you will be told. Teach
  from the top of that page.
- "Explain this again", "I don't understand": explain the SAME idea a different way — smaller steps,
  a new example, the thing underneath it. Never repeat the same words.
- If they ask any question, answer it directly and simply, then carry on where you were.
- Interruption: the moment they speak, stop and listen.

====================  CHECKING UNDERSTANDING  ====================
- Ask a short question only where it really helps — after a key idea or a worked example, roughly
  once per page, and more when they seem confused. Never fire a question after every paragraph.
- Wait for their answer. Give short, specific feedback. If they cannot answer after two tries, give a
  hint, then the answer with the reason, and move on.
- Never say "correct" before you have checked their answer.
{quiz}{resume}
====================  HOW YOU SOUND  ====================
Short sentences. Everyday Nigerian classroom English. One idea per sentence. Encouraging, never
condescending. Never leave dead air: end each turn with a question or a clear next step.
Read maths aloud naturally ("x squared", "two x plus three").

====================  THE NOTES (on the board)  ====================
Block ids like [p2b3] mean page 2, block 3.
{notes}
"""


# ── The board assistant (for things NOT in the notes) ───────────────────
_BOARD_HAND = """You are the board assistant for StudyMaster, a live teacher giving a {subject} lesson on
"{topic}". The full lesson notes are already on the board, page by page (index below). You act only
when the teacher hands off to you.

- go_to_page(page) when the teacher or student wants a different page.
- write_on_board for something NOT in the notes: the answer to the student's own question, a
  correction, the student's numbers. Short lines; maths in $...$ with single backslashes.
- show_image(query) only when a real photo/diagram genuinely helps and none is in the notes.
- If nothing needs to change, call no tool.

Any text you output is SPOKEN by the teacher — output NO text at all. Never write the teacher's
conversational words onto the board.

PAGES:
{index}"""

BOARD_TOOLS = [
    {
        "type": "function",
        "name": "go_to_page",
        "description": "Turn the board to a page of the notes (0 = objectives; the quiz page is last).",
        "parameters": {"type": "object", "properties": {"page": {"type": "integer"}}, "required": ["page"]},
    },
    {
        "type": "function",
        "name": "write_on_board",
        "description": "Add something NOT in the notes to the current page. Short lines; maths in $...$.",
        "parameters": {
            "type": "object",
            "properties": {
                "heading": {"type": "string"},
                "lines": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["lines"],
        },
    },
    {
        "type": "function",
        "name": "show_image",
        "description": "Find and show a real photo or diagram from Wikipedia/Wikimedia.",
        "parameters": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]},
    },
]


def delegation_config(*, subject: str, topic: str, page_titles: list[str]) -> dict[str, Any]:
    index = "\n".join(f"page {i}: {t}" for i, t in enumerate(page_titles))
    responses: dict[str, Any] = {
        "model": settings.openai_delegate_model,
        "instructions": _BOARD_HAND.format(subject=subject, topic=topic, index=index),
        "tools": BOARD_TOOLS,
        "tool_choice": "auto",
        "parallel_tool_calls": False,
        # measured: none/priority ~1.0s vs low/default ~3.1s for these calls
        "reasoning": {"effort": settings.openai_delegate_reasoning},
        "text": {"verbosity": "low"},
    }
    if settings.openai_delegate_service_tier:
        responses["service_tier"] = settings.openai_delegate_service_tier
    return {"type": "responses", "responses": responses}


async def create_live_session(*, sdp: str, instructions: str,
                              delegation: dict[str, Any]) -> dict[str, Any]:
    if not settings.openai_api_key:
        raise RuntimeError("OPENAI_API_KEY is not configured")
    payload = {
        "session": {
            "model": settings.openai_live_model,
            "instructions": instructions,
            "audio": {"output": {"voice": settings.openai_live_voice}},
            "delegation": delegation,
            "store": False,
        },
        "transport": {"type": "webrtc", "sdp": sdp},
    }
    async with httpx.AsyncClient(timeout=60) as client:
        response = await client.post(
            f"{OPENAI_BASE}/live/sessions",
            headers={"Authorization": f"Bearer {settings.openai_api_key}"},
            json=payload,
        )
    if response.is_error:
        logger.error("GPT-Live session failed (%s): %s", response.status_code, response.text[:500])
        raise RuntimeError("Could not start the live teacher")
    return response.json()
