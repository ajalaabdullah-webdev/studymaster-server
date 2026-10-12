"""Agentic note generation.

A multi-step pipeline (not a single LLM call) that streams its progress so the
note canvas can show what it's doing:

    plan → search curriculum → check question bank → write (HTML+LaTeX)
         → find & verify images (Wikimedia) → assemble → build quiz → save

`emit` is an async callback used to stream progress events to the SSE endpoint.
It is optional so the agent is unit-testable without a request.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Optional

from beanie import PydanticObjectId

from app.core.llm import chat, chat_json
from app.core.subjects import to_slug
from app.models.activity import Activity
from app.models.note import ChatTurn, Note, NoteImage, NoteVersion
from app.models.question import Question
from app.models.quiz import Quiz, QuizQuestion
from app.services.search import format_results_for_llm, web_search
from app.services.wikimedia import search_images

logger = logging.getLogger("studymaster.note_agent")

Emit = Callable[[dict[str, Any]], Awaitable[None]]
MAX_IMAGES = 5
IMG_TOKEN = re.compile(r"\{\{\s*IMG:\s*(.+?)\s*\}\}", re.IGNORECASE)
_FENCE = re.compile(r"^\s*```(?:html)?\s*|\s*```\s*$", re.IGNORECASE)


def _strip_fence(html: str) -> str:
    """Remove a markdown ```html ... ``` wrapper the LLM sometimes adds."""
    return _FENCE.sub("", html or "").strip()


async def _noop(_: dict[str, Any]) -> None:  # default emitter
    return None


async def _progress(emit: Emit, message: str, step: str = "") -> None:
    await emit({"type": "progress", "message": message, "step": step})


async def generate_note(
    *,
    user_id: PydanticObjectId,
    prompt: str,
    onboarding: Optional[dict[str, Any]] = None,
    image_data_urls: Optional[list[str]] = None,
    emit: Emit | None = None,
) -> dict[str, Any]:
    emit = emit or _noop
    onboarding = onboarding or {}
    user_class = onboarding.get("class") or "WAEC/JAMB"

    # 1) PLAN
    await _progress(emit, "Understanding your topic…", "plan")
    plan = await _plan(prompt, user_class, image_data_urls)
    title = plan.get("title") or prompt[:80]
    subject = to_slug(plan.get("subject") or "")
    topic = plan.get("topic") or title
    await emit({"type": "meta", "title": title, "subject": subject, "topic": topic})

    # 2) SEARCH CURRICULUM
    await _progress(emit, "Searching the Nigerian curriculum…", "search")
    search = await web_search(
        f"{topic} explained for Nigerian WAEC JAMB students. Key points and syllabus for {topic}.",
        max_results=5,
    )

    # 3) QUESTION BANK
    await _progress(emit, "Checking our question bank for related questions…", "questions")
    related = await _related_questions(subject, topic)

    # 4) WRITE
    await _progress(emit, "Writing your note…", "write")
    content = await _write_note(
        title=title,
        topic=topic,
        user_class=user_class,
        search_text=format_results_for_llm(search),
        related=related,
        plan=plan,
    )

    # 5) IMAGES
    images: list[NoteImage] = []
    if plan.get("useImages", True):
        await _progress(emit, "Finding helpful images…", "images")
        content, images = await _attach_images(content, topic, subject)

    # 6) ASSEMBLE / clean
    await _progress(emit, "Double-checking your note…", "verify")
    content = IMG_TOKEN.sub("", content).strip()  # drop any unfilled tokens

    # 7) QUIZ
    await _progress(emit, "Creating a quiz to test you…", "quiz")
    quiz_questions = await _build_quiz(content, topic)

    # 8) SAVE
    await _progress(emit, "Saving to your library…", "save")
    summary = await _summary_generate(title, plan.get("sections", []), len(images), quiz_questions)
    note = Note(
        userId=user_id,
        title=title,
        content=content,
        tag=(subject or "General"),
        subject=subject,
        topic=topic,
        images=images,
        chatHistory=[
            ChatTurn(role="user", message=prompt),
            ChatTurn(role="assistant", message=summary, versionNumber=1),
        ],
        versions=[],
        currentVersion=1,
    )
    await note.insert()

    quiz_id = None
    if quiz_questions:
        quiz = Quiz(
            userId=user_id,
            noteTitle=title[:100],
            noteId=note.id,
            questions=[QuizQuestion(**q) for q in quiz_questions],
        )
        await quiz.insert()
        quiz_id = quiz.id
        note.quizId = quiz_id
    note.versions = [NoteVersion(
        number=1, content=content, images=images, quizId=quiz_id,
        instruction=prompt,
    )]
    await note.save()

    await Activity(
        userId=user_id, type="note_creation", status="completed",
        endTime=datetime.now(timezone.utc), metadata={"noteId": str(note.id), "title": title},
    ).insert()

    result = {
        "noteId": str(note.id),
        "id": str(note.id),
        "title": title,
        "content": content,
        "subject": subject,
        "topic": topic,
        "images": [img.model_dump() for img in images],
        "quizId": str(quiz_id) if quiz_id else None,
        # Correct answers stay server-side; the client marks via the submit endpoint.
        "quiz": [{"question": q["question"], "options": q["options"]} for q in quiz_questions],
        "chatHistory": [t.model_dump(mode="json") for t in note.chatHistory],
        "versions": [v.model_dump(mode="json") for v in note.versions],
        "currentVersion": note.currentVersion,
    }
    await emit({"type": "complete", "note": result})
    await emit({"type": "assistant_message", "message": summary})
    return result


# --------------------------------------------------------------------------- #
# steps
# --------------------------------------------------------------------------- #
async def _plan(prompt: str, user_class: str, image_data_urls: Optional[list[str]]) -> dict[str, Any]:
    content: Any = f"""A {user_class} student asked for a study note:
"{prompt}"

Plan the note. Return STRICT JSON:
{{
  "title": "clear note title",
  "subject": "the school subject (e.g. Biology, Mathematics, Government) or empty",
  "topic": "the specific topic",
  "useImages": true,
    "sections": ["Introduction", "Key concept 1", "..."],
    "nextStep": "the specific next learning module after this note and its quiz"
}}"""
    messages: list[dict[str, Any]] = [{"role": "user", "content": content}]
    # multimodal: attach uploaded images so the plan/topic can reference them
    if image_data_urls:
        parts = [{"type": "text", "text": content}]
        for url in image_data_urls[:4]:
            parts.append({"type": "image_url", "image_url": {"url": url}})
        messages = [{"role": "user", "content": parts}]
    try:
        data = await chat_json(messages, tier="default", temperature=0.3)
        return data if isinstance(data, dict) else {}
    except Exception as exc:  # noqa: BLE001
        logger.error("note plan failed: %s", exc)
        return {"title": prompt[:80], "useImages": True}


async def _related_questions(subject: Optional[str], topic: str) -> str:
    if not subject:
        return ""
    docs = (
        await Question.find(Question.subject == subject, Question.question_type == "obj")
        .limit(3)
        .to_list()
    )
    if not docs:
        return ""
    lines = [f"- {d.question_text[:160]}" for d in docs]
    return "Example exam questions in this subject (align the note to this style):\n" + "\n".join(lines)


async def _write_note(*, title, topic, user_class, search_text, related, plan) -> str:
    sections = ", ".join(plan.get("sections", []) or [])
    prompt = f"""You are the best, warmest teacher in Nigeria, explaining a topic to a {user_class}
student who finds textbooks hard and boring. Teach — don't lecture. This note must NOT read like a
textbook; it should feel like a patient teacher sitting beside the student explaining clearly.

Topic: {topic}   (Title: {title})
Cover these ideas: {sections}

Use this research for accuracy (fix any errors, never copy word-for-word):
{search_text}

{related}

HOW TO WRITE IT:
- Start by saying, in one or two friendly sentences, what this topic is and why it matters — in plain words.
- Explain every idea in SIMPLE language. Short sentences. No big grammar. If you must use a hard/technical
  word, immediately explain it in brackets in everyday terms.
- Use real Nigerian, everyday analogies and examples (market, football, jollof, danfo, etc.) to make abstract
  ideas click.
- Build up step by step, from easy to harder. Assume the student is smart but starting fresh.
- Include worked examples with the steps shown, where relevant.
- Teach the student's requested learning objective completely. Do not squeeze it into a shallow overview or
  stop because the note has reached an arbitrary length. Keep it focused by limiting the scope, not the depth.
- Warm, encouraging tone ("Don't worry, this is easier than it looks", "Notice how...").

FORMAT:
1. Return ONLY an HTML fragment (no <html>/<body>, no markdown code fences).
   Use <h1> for the title, <h2>/<h3> for sections, <p>, <ul>/<li>, and <strong> for key terms.
2. Use LaTeX for ALL maths: $...$ inline, $$...$$ for display formulae.
3. Where a picture or diagram would genuinely help understanding, put a placeholder ALONE on its own line:
   {{{{IMG: a specific image search query}}}}
   Use 3 to 5 of these, spread through the note, ONLY where an image truly aids understanding.
4. End with a short, plain-English "Summary" (the key things to remember).
5. End with a "What to do next" section. Tell the student to take the quiz, then recommend this specific next
   note: {plan.get("nextStep") or "the next logical skill that builds on this note"}."""
    html = await chat([{"role": "user", "content": prompt}], tier="smart", temperature=0.55, max_tokens=5000)
    return _strip_fence(html)


async def _attach_images(content: str, topic: str, subject: Optional[str]) -> tuple[str, list[NoteImage]]:
    queries = IMG_TOKEN.findall(content)[:MAX_IMAGES]
    images: list[NoteImage] = []
    used_urls: set[str] = set()
    for query in queries:
        token_re = re.compile(r"\{\{\s*IMG:\s*" + re.escape(query) + r"\s*\}\}", re.IGNORECASE)
        chosen = await _find_best_image(query, topic, used_urls)
        if chosen:
            used_urls.add(chosen["url"])
            caption = chosen.get("caption") or chosen.get("description") or chosen.get("title") or query
            fig = (
                f'<figure class="note-image">'
                f'<img src="{chosen["url"]}" alt="{_esc(chosen.get("title", query))}" loading="lazy"/>'
                f'<figcaption>{_esc(caption)}</figcaption>'
                f"</figure>"
            )
            content = token_re.sub(fig, content, count=1)
            images.append(
                NoteImage(
                    url=chosen["url"], title=chosen.get("title", ""),
                    description=caption,
                    pageUrl=chosen.get("pageUrl", ""), license=chosen.get("license", ""),
                )
            )
        else:
            content = token_re.sub("", content, count=1)
    return content, images


async def _find_best_image(query: str, topic: str, used_urls: set[str]) -> Optional[dict]:
    """Search, review candidates for genuine relevance, and if none fit, retry with
    a refined query. Returns the chosen candidate (with a written caption) or None."""
    search_terms = [f"{query} {topic}".strip(), query, f"{topic} {query} diagram"]
    for attempt, term in enumerate(search_terms):
        candidates = [c for c in await search_images(term, limit=8) if c["url"] not in used_urls]
        if not candidates:
            continue
        chosen = await _review_images(query, topic, candidates)
        if chosen:
            return chosen
        # else loop to the next, more specific search term
    return None


async def _review_images(query: str, topic: str, candidates: list[dict]) -> Optional[dict]:
    """Have the agent genuinely review the candidates: pick the ONE that best and
    accurately illustrates the point, and write a short caption. Reject weak sets."""
    if not candidates:
        return None
    listing = "\n".join(
        f'{i}. title="{c.get("title", "")}" | description="{(c.get("description") or "")[:160]}"'
        for i, c in enumerate(candidates)
    )
    try:
        data = await chat_json(
            [{"role": "user", "content": f"""We are illustrating a study note on "{topic}".
We need an image that clearly and accurately shows: "{query}".

Review these candidates strictly — like a teacher choosing a diagram for class. It must genuinely and
correctly depict the concept (a real diagram/photo of the thing), be clear, and be appropriate. Reject
logos, unrelated photos, memes, or anything only loosely related.

Candidates:
{listing}

Return JSON:
{{"index": <index of the best image, or -1 if NONE are good enough>,
  "caption": "<one short sentence describing what the image shows, for the student>"}}"""}],
            tier="default", temperature=0.0,
        )
        if not isinstance(data, dict):
            return None
        idx = int(data.get("index", -1))
        if 0 <= idx < len(candidates):
            chosen = dict(candidates[idx])
            chosen["caption"] = (data.get("caption") or "").strip() or chosen.get("description") or query
            return chosen
        return None
    except Exception:  # noqa: BLE001
        return None


async def _build_quiz(content: str, topic: str) -> list[dict[str, Any]]:
    text = re.sub(r"<[^>]+>", " ", content)[:2600]
    try:
        data = await chat_json(
            [{"role": "user", "content": f"""From this note on "{topic}", write 8 multiple-choice questions.
NOTE:
{text}

Return STRICT JSON array; each: {{"question": str, "options": [4 strings],
"correctAnswer": "<the exact correct option text>", "explanation": str}}. Use $...$ for any maths."""}],
            tier="default", temperature=0.4,
        )
        if isinstance(data, dict):
            data = data.get("questions") or next((v for v in data.values() if isinstance(v, list)), [])
        out = []
        for q in data or []:
            if q.get("question") and len(q.get("options", [])) >= 2 and q.get("correctAnswer"):
                out.append({
                    "question": q["question"], "options": q["options"],
                    "correctAnswer": q["correctAnswer"], "explanation": q.get("explanation", ""),
                })
        return out[:10]
    except Exception as exc:  # noqa: BLE001
        logger.error("quiz build failed: %s", exc)
        return []


async def edit_note(
    *, note: Note, instruction: str, source_version: int | None = None,
    emit: Emit | None = None,
) -> dict[str, Any]:
    """Create a new immutable version from the requested version of a note."""
    emit = emit or _noop
    if not note.versions:
        note.versions = [NoteVersion(
            number=1, content=note.content, images=list(note.images or []),
            instruction="Original note", quizId=note.quizId,
        )]
        for turn in reversed(note.chatHistory):
            if turn.role == "assistant":
                turn.versionNumber = 1
                break

    source = next((v for v in note.versions if v.number == source_version), None)
    if source is None:
        source = max(note.versions, key=lambda v: v.number)
    next_version = max(v.number for v in note.versions) + 1

    await _progress(emit, "Reading your note…", "edit")
    journey_parts = []
    for version in sorted(note.versions, key=lambda item: item.number):
        plain = re.sub(r"<[^>]+>", " ", version.content)
        plain = re.sub(r"\s+", " ", plain).strip()[:3500]
        journey_parts.append(
            f"VERSION {version.number} — request: {version.instruction}\n{plain}"
        )
    journey = "\n\n".join(journey_parts)[-14000:]

    await _progress(emit, f"Creating Version {next_version}…", "edit")
    new_html = await chat(
        [{"role": "user", "content": f"""You are a warm Nigerian teacher continuing a student's learning journey.
Create Version {next_version} in response to the student's request. You can see what every earlier version
taught, so do not restart from the beginning or repeat material the student already understands.

DECIDE THE RIGHT KIND OF VERSION:
- If the student asks to continue, go deeper, focus on calculations, or learn the next part, write a NEW,
  self-contained learning module focused on that request. Use only a short bridge from previous knowledge.
- If the student explicitly asks to correct, simplify, reformat, add images to, or rewrite the selected version,
  produce a revised replacement of that selected material.
- Follow the student's stated knowledge. If they say they know the basics, do not reteach the basics.

Teach the requested objective completely with clear reasoning and fully worked steps. Keep the scope focused,
but never compress it into a brief overview just to fit a length. Use short sentences, explain hard words in
brackets, and use useful Nigerian examples. Use {{{{IMG: specific query}}}} placeholders only where helpful.

Return ONLY the full updated HTML fragment (no markdown fences). Use $...$ for maths.
End with a short Summary, then a "What to do next" section that tells the student to take this version's quiz
and recommends one specific next note or skill.

STUDENT'S REQUEST: {instruction}

SELECTED VERSION {source.number}:
{source.content}

LEARNING JOURNEY SO FAR:
{journey}"""}],
        tier="smart", temperature=0.45, max_tokens=6000,
    )
    content = _strip_fence(new_html)
    version_images = list(source.images or [])

    # If the edit introduced image placeholders, fulfil them.
    if IMG_TOKEN.search(content):
        await _progress(emit, "Finding better images…", "images")
        content, new_imgs = await _attach_images(content, note.topic or note.title, note.subject)
        if new_imgs:
            version_images = version_images + new_imgs
    content = IMG_TOKEN.sub("", content).strip()

    await _progress(emit, f"Creating a quiz for Version {next_version}…", "quiz")
    quiz_questions = await _build_quiz(content, note.topic or note.title)
    quiz_id = None
    if quiz_questions:
        quiz = Quiz(
            userId=note.userId,
            noteTitle=f"{note.title} — Version {next_version}"[:100],
            noteId=note.id,
            questions=[QuizQuestion(**q) for q in quiz_questions],
        )
        await quiz.insert()
        quiz_id = quiz.id

    note.content = content
    note.images = version_images
    note.versions.append(NoteVersion(
        number=next_version,
        content=content,
        images=version_images,
        instruction=instruction,
        basedOnVersion=source.number,
        quizId=quiz_id,
    ))
    note.currentVersion = next_version
    note.quizId = quiz_id
    note.audioUrl = None
    note.audioProvider = None
    note.audioVersion = None
    note.readAlong = None  # invalidate cached read-along
    note.updatedAt = datetime.now(timezone.utc)

    summary = await _summary_edit(instruction, note.title, next_version)
    note.chatHistory.append(ChatTurn(role="user", message=instruction))
    note.chatHistory.append(ChatTurn(
        role="assistant", message=summary, versionNumber=next_version,
    ))
    await note.save()

    result = {
        "noteId": str(note.id), "id": str(note.id), "title": note.title,
        "content": note.content, "subject": note.subject, "topic": note.topic,
        "images": [i.model_dump() for i in note.images],
        "quizId": str(note.quizId) if note.quizId else None,
        "quiz": [{"question": q["question"], "options": q["options"]} for q in quiz_questions],
        "chatHistory": [t.model_dump(mode="json") for t in note.chatHistory],
        "versions": [v.model_dump(mode="json") for v in note.versions],
        "currentVersion": note.currentVersion,
    }
    await emit({"type": "complete", "note": result})
    await emit({"type": "assistant_message", "message": summary})
    return result


async def answer_question(
    *, prompt: str, note: Optional[Note], onboarding: dict[str, Any], emit: Emit | None = None
) -> dict[str, Any]:
    """Answer the student's question conversationally (does NOT change the note)."""
    emit = emit or _noop
    user_class = onboarding.get("class") or "SS3"
    context = ""
    if note:
        plain = re.sub(r"<[^>]+>", " ", note.content)[:2500]
        context = f'The student is looking at their note titled "{note.title}". Note content:\n{plain}\n'
    answer = await chat(
        [{"role": "user", "content": f"""You are a warm, patient Nigerian teacher helping a {user_class} student.
{context}
The student asks: "{prompt}"

Answer clearly and SIMPLY, like explaining to a friend. Short sentences, plain words, a local example if it
helps. Use $...$ for any maths. Keep it focused and encouraging. Return plain text/markdown (no HTML)."""}],
        tier="smart", temperature=0.5, max_tokens=800,
    )
    if note:
        note.chatHistory.append(ChatTurn(role="user", message=prompt))
        note.chatHistory.append(ChatTurn(role="assistant", message=answer))
        await note.save()
    await emit({"type": "answer", "message": answer})
    await emit({"type": "assistant_message", "message": answer})
    return {"answer": answer}


async def classify_intent(prompt: str, *, note_open: bool, note_title: str = "") -> str:
    """Decide whether the student wants to GENERATE a new note, EDIT the open note,
    or just get an ANSWER to a question. Returns 'generate' | 'edit' | 'answer'."""
    ctx = (
        f'A note titled "{note_title}" is currently open on screen.'
        if note_open else "No note is currently open."
    )
    try:
        data = await chat_json(
            [{"role": "user", "content": f"""{ctx}

The student typed: "{prompt}"

Classify what they want:
- "generate": they want a NEW note created on a topic (e.g. "make a note on cell division", "notes on osmosis").
- "edit": they want to CHANGE the note currently open (e.g. "add more images", "make it simpler",
  "break down the big words", "add examples", "explain this part more"). Only possible if a note is open.
- "answer": they are asking a question to be answered in chat (e.g. "what does osmosis mean?",
  "why is the sky blue?", "explain diffusion") without asking to create or change a note.

Return JSON: {{"intent": "generate" | "edit" | "answer"}}"""}],
            tier="default", temperature=0.0,
        )
        intent = (data.get("intent") if isinstance(data, dict) else "") or ""
        intent = intent.strip().lower()
        if intent not in {"generate", "edit", "answer"}:
            intent = "edit" if note_open else "generate"
        if intent == "edit" and not note_open:
            intent = "generate"
        return intent
    except Exception:  # noqa: BLE001
        return "edit" if note_open else "generate"


async def _summary_generate(title: str, sections: list, image_count: int, quiz: list) -> str:
    secs = ", ".join(sections[:5]) if sections else title
    try:
        return await chat(
            [{"role": "user", "content": f"""Write a warm, short 1-2 sentence chat message to a student telling them
you've just made them a simple note titled "{title}" covering {secs}
({image_count} image(s) and a {len(quiz)}-question quiz included). Invite them to read it or ask you to explain
or change anything. Sound human and encouraging. Plain text only."""}],
            tier="default", temperature=0.6, max_tokens=120,
        )
    except Exception:  # noqa: BLE001
        return f"I've made you a simple note on {title}, with a quiz to practise. Have a read — tell me if you'd like me to explain or change anything!"


async def _summary_edit(instruction: str, title: str, version_number: int) -> str:
    try:
        return await chat(
            [{"role": "user", "content": f"""In one short, friendly sentence, tell the student you created Version
{version_number} of their learning journey for "{title}" in response to "{instruction}". Mention that this
version has its own quiz. Plain text only."""}],
            tier="default", temperature=0.6, max_tokens=80,
        )
    except Exception:  # noqa: BLE001
        return f"Version {version_number} is ready with its own quiz. Take a look!"


def _esc(s: str) -> str:
    return (s or "").replace('"', "&quot;").replace("<", "&lt;").replace(">", "&gt;")
