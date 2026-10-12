"""The lesson notebook — the full notes written BEFORE the live class starts.

Why this exists
---------------
Students testing the live class said it did not feel like a teacher: it opened
with "what are you stuck on?" and improvised. What they wanted is what a good
teacher does — arrive with the lesson prepared, put the notes on the board, and
teach them paragraph by paragraph, answering questions along the way.

So the class now starts with this module. From the student's setup form it
writes, in parallel:

- an objectives page,
- 4-6 note pages (explanations, definitions, lists, formulas and FULL worked
  examples — every step of the maths), each with at most one checked picture,
- a final quiz.

The live voice session only starts once this is done, and the whole notebook
goes into the teacher's instructions — she knows every paragraph from her first
word, so the board can never lag behind her.

Block ids (``p2b3`` = page 2, block 3) are how the teacher, the board and the
browser refer to a paragraph.
"""

from __future__ import annotations

import asyncio
import logging
import re
from typing import Any

from app.config import settings
from app.core import llm
from app.services.lesson_pack import _clean, pick_image

logger = logging.getLogger("studymaster.notebook")

# lessonId -> progress, read by the preparing screen while the notes are written.
PROGRESS: dict[str, dict[str, Any]] = {}


async def _luna(prompt: str, system: str, max_tokens: int = 8000) -> dict[str, Any]:
    """Accuracy matters more than speed here: gpt-6-luna, shallow reasoning."""
    return await llm.chat_json(
        [{"role": "system", "content": system}, {"role": "user", "content": prompt}],
        tier="smart", model=settings.lesson_pack_model, temperature=1,
        reasoning_effort="low", max_completion_tokens=max_tokens,
    )


async def _outline(*, subject: str, topic: str, goal: str, level: str, details: str,
                   minutes: int) -> dict[str, Any]:
    pages = 4 if minutes <= 20 else 5 if minutes <= 40 else 6
    prompt = f"""Plan the notes for a live one-to-one {subject} lesson on "{topic}" for a Nigerian
WAEC/JAMB candidate. Lesson length: {minutes} minutes. Their level: {level}. Their goal: {goal}.
What the student told us (shape the lesson around this): {details or 'nothing extra'}

Return strict JSON:
{{"objectives": ["3-5 things the student will be able to do by the end, starting with a verb"],
  "pages": [{{"title": "short page title", "covers": "one line: exactly what this page teaches"}}]}}

Exactly {pages} pages that build on each other, in teaching order, covering what WAEC/JAMB examine on
this topic, with extra depth where the student said they struggle. Titles are plain, as a student
would say them."""
    # The outline is a short plan, not content: a fast model is enough, and the
    # student is watching the preparing screen. The pages stay on gpt-6-luna.
    return await llm.chat_json(
        [{"role": "system", "content": "You plan clear, exam-focused lessons."},
         {"role": "user", "content": prompt}],
        tier="smart", temperature=0.3, max_tokens=900,
    )


_PAGE_PROMPT = """You are writing ONE page of the notes for a live {subject} lesson on "{topic}" for a
Nigerian WAEC/JAMB candidate (level: {level}). A teacher will teach these notes from a board,
paragraph by paragraph. What the student told us: {details}

The whole lesson: {plan}
THIS PAGE ({number} of {count}): "{title}" — {covers}

Write this page's notes as 4-8 blocks in teaching order. Block kinds:
- {{"kind":"paragraph","text":"2-4 clear sentences explaining an idea"}}
- {{"kind":"definition","term":"...","text":"the exact definition"}}
- {{"kind":"list","title":"...","items":["...","..."]}}
- {{"kind":"formula","label":"...","latex":"LaTeX with NO dollar signs","explanation":"what each symbol means"}}
- {{"kind":"example","title":"Worked example","problem":"a WAEC/JAMB-style question","steps":["every step of the working, one per line"],"answer":"final answer"}}
- {{"kind":"subheading","text":"..."}}

Rules:
- These are FULL notes a student can revise from, not a summary. Explain WHY, not just what.
- Mathematics and calculations: break down the ENTIRE working — every step on its own line, never
  skip a step, and verify every number. Maths inline as $...$ (single backslashes).
- Use Nigerian everyday examples where natural. Never use an example that is itself a classic
  misconception (e.g. "breathing" as an example of respiration).
- Cover only this page; do not repeat other pages.
- "imageQuery": the name of ONE concrete thing whose Wikipedia article picture would genuinely help
  on this page (e.g. "Amoeba", "Plant cell", "Simple pendulum"); empty string if a picture would not
  help (most algebra, grammar).

Return strict JSON: {{"blocks":[...], "imageQuery":"..."}}"""


def _block_ops(block: dict[str, Any]) -> dict[str, Any] | None:
    """One note block -> one board operation the board already knows how to draw."""
    kind = block.get("kind")
    if kind == "paragraph" and _clean(block.get("text")):
        return {"action": "write_text", "content": {"text": _clean(block.get("text"), 900)}}
    if kind == "definition" and _clean(block.get("text")):
        term = _clean(block.get("term"), 80)
        text = _clean(block.get("text"), 600)
        return {"action": "write_text", "emphasis": "key",
                "content": {"text": f"{term}: {text}" if term else text, "emphasis": "key"}}
    if kind == "subheading" and _clean(block.get("text")):
        return {"action": "write_heading", "content": {"text": _clean(block.get("text"), 100)}}
    if kind == "list":
        items = [_clean(x, 300) for x in block.get("items") or [] if _clean(x)][:10]
        if items:
            return {"action": "write_steps", "content": {"title": _clean(block.get("title"), 100), "steps": items}}
    if kind == "formula" and block.get("latex"):
        return {"action": "write_formula", "emphasis": "key", "content": {
            "label": _clean(block.get("label"), 100),
            "latex": _clean(block.get("latex"), 600).strip("$"),
            "explanation": _clean(block.get("explanation"), 400),
        }}
    if kind == "example" and _clean(block.get("problem")):
        steps = [_clean(x, 300) for x in block.get("steps") or [] if _clean(x)][:14]
        answer = _clean(block.get("answer"), 200)
        if answer:
            steps.append(f"Answer: {answer}")
        return {"action": "show_example", "content": {
            "title": _clean(block.get("title"), 100) or "Worked example",
            "problem": _clean(block.get("problem"), 600), "solution": steps,
        }}
    return None


async def _page(*, n: int, count: int, page: dict[str, str], plan: str, subject: str, topic: str,
                level: str, details: str, lesson_id: str) -> dict[str, Any]:
    try:
        data = await _luna(_PAGE_PROMPT.format(
            subject=subject, topic=topic, level=level, details=details or "nothing extra",
            plan=plan, number=n, count=count, title=page["title"], covers=page.get("covers", "")),
            "You write accurate, complete exam notes for Nigerian secondary-school students.")
    except Exception:
        logger.exception("page %s failed", n)
        data = {}
    ops = [op for op in (_block_ops(b) for b in data.get("blocks") or [] if isinstance(b, dict)) if op]
    query = _clean(data.get("imageQuery"), 60)
    if query and ops:
        _bump(lesson_id, f"Finding a picture for page {n}")
        picture = await pick_image(query, purpose=f"{page['title']} ({topic})")
        if picture:
            # where a teacher would hold it up: after the first explanation
            ops.insert(min(1, len(ops)), {"action": "show_image", "content": picture})
    _bump(lesson_id, f"Page {n} written", done=1)
    return {"title": page["title"], "ops": ops}


async def _quiz(*, subject: str, topic: str, plan: str, level: str) -> list[dict[str, Any]]:
    prompt = f"""Write the end-of-lesson quiz for a live {subject} lesson on "{topic}" (level: {level}).
The lesson covered: {plan}

5 multiple-choice questions in WAEC/JAMB style, spread across the pages, easy to hard. Wrong options
are mistakes real students make. Maths inline as $...$. Verify every answer.
Return strict JSON: {{"questions":[{{"question":"...","options":["A","B","C","D"],"correctIndex":0,
"explanations":["one line per option: why it is right, or the mistake it represents"]}}]}}"""
    try:
        data = await _luna(prompt, "You write accurate exam-style quizzes.", max_tokens=5000)
    except Exception:
        logger.exception("quiz failed")
        return []
    out = []
    for q in (data.get("questions") or [])[:6]:
        options = [_clean(o, 200) for o in q.get("options") or [] if _clean(o)][:4]
        if len(options) < 2 or not _clean(q.get("question")):
            continue
        try:
            correct = min(max(int(q.get("correctIndex", 0)), 0), len(options) - 1)
        except (TypeError, ValueError):
            correct = 0
        out.append({"question": _clean(q.get("question"), 500), "options": options, "correctIndex": correct,
                    "explanations": [_clean(e, 300) for e in q.get("explanations") or []][:4]})
    return out


def _bump(lesson_id: str, stage: str, done: int = 0) -> None:
    p = PROGRESS.setdefault(lesson_id, {"stage": "", "done": 0, "total": 1})
    p["stage"] = stage
    p["done"] += done


def _number(pages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Give every block a stable id: p<page>b<block> (page 0 = objectives)."""
    for n, page in enumerate(pages):
        for b, op in enumerate(page["ops"], start=1):
            op["id"] = f"p{n}b{b}"
            op.setdefault("emphasis", "normal")
    return pages


async def build(*, lesson_id: str, subject: str, topic: str, goal: str, level: str,
                details: str, minutes: int) -> dict[str, Any]:
    """Write the whole notebook. Never raises; an empty result means it failed."""
    PROGRESS[lesson_id] = {"stage": "Planning the lesson", "done": 0, "total": 1}
    try:
        outline = await _outline(subject=subject, topic=topic, goal=goal, level=level,
                                 details=details, minutes=minutes)
    except Exception:
        logger.exception("outline failed")
        return {}
    planned = [{"title": _clean(p.get("title"), 80), "covers": _clean(p.get("covers"), 200)}
               for p in outline.get("pages") or [] if _clean(p.get("title"))][:6]
    objectives = [_clean(o, 200) for o in outline.get("objectives") or [] if _clean(o)][:5]
    if not planned:
        return {}
    PROGRESS[lesson_id].update(total=len(planned) + 1, stage=f"Writing {len(planned)} pages of notes")
    plan = "; ".join(f"page {i + 1}: {p['title']} ({p['covers']})" for i, p in enumerate(planned))

    async def quiz_task() -> list[dict[str, Any]]:
        questions = await _quiz(subject=subject, topic=topic, plan=plan, level=level)
        _bump(lesson_id, "Quiz ready", done=1)
        return questions

    *pages, quiz = await asyncio.gather(
        *[_page(n=i + 1, count=len(planned), page=p, plan=plan, subject=subject, topic=topic,
                level=level, details=details, lesson_id=lesson_id) for i, p in enumerate(planned)],
        quiz_task(),
    )
    pages = [p for p in pages if p["ops"]]
    if not pages:
        return {}
    # Pages are written in parallel, so several can pick the same picture
    # (one test showed the chloroplast three times). Keep each picture once.
    seen: set[str] = set()
    for p in pages:
        kept = []
        for op in p["ops"]:
            url = op["content"].get("url") if op["action"] == "show_image" else None
            if url and url in seen:
                continue
            if url:
                seen.add(url)
            kept.append(op)
        p["ops"] = kept
    intro = {"title": "Objectives", "ops": [
        {"action": "set_focus", "content": {"title": topic, "objective": goal}},
        {"action": "write_steps", "content": {"title": "By the end of this lesson you will be able to:",
                                              "steps": objectives or [f"Understand {topic}"]}},
        {"action": "write_steps", "content": {"title": "What we will cover",
                                              "steps": [p["title"] for p in pages]}},
    ]}
    quiz_page = {"title": "Quiz", "ops": [
        {"action": "show_question", "content": {**q, "quizIndex": i}} for i, q in enumerate(quiz)
    ]} if quiz else None
    notebook_pages = _number([intro, *pages] + ([quiz_page] if quiz_page else []))
    PROGRESS[lesson_id].update(stage="Ready", done=PROGRESS[lesson_id]["total"])
    return {"objectives": objectives, "pages": notebook_pages, "hasQuiz": bool(quiz_page)}


def page_text(op: dict[str, Any]) -> str:
    """Plain words of one block, for the teacher's copy of the notes."""
    c = op.get("content") or {}
    action = op.get("action")
    if action == "write_steps":
        return f"{c.get('title', '')} " + "; ".join(c.get("steps") or [])
    if action == "show_example":
        return f"{c.get('title', '')}: {c.get('problem', '')} Working: " + " | ".join(c.get("solution") or [])
    if action == "write_formula":
        return f"{c.get('label', '')} ${c.get('latex', '')}$ {c.get('explanation', '')}"
    if action == "show_image":
        return f"[PICTURE] {c.get('caption', '')}"
    if action == "show_question":
        letters = "ABCD"
        opts = " ".join(f"{letters[i]}) {o}" for i, o in enumerate(c.get("options") or []))
        return f"[QUIZ Q{c.get('quizIndex', 0) + 1}] {c.get('question', '')} {opts} (answer {letters[c.get('correctIndex', 0)]})"
    if action == "set_focus":
        return f"{c.get('title', '')} — {c.get('objective', '')}"
    return str(c.get("text", ""))


def notebook_text(pages: list[dict[str, Any]]) -> str:
    """The notes as the teacher reads them in her instructions."""
    out = []
    for n, page in enumerate(pages):
        label = "PAGE 0 (objectives)" if n == 0 else ("QUIZ PAGE" if page["title"] == "Quiz" else f"PAGE {n}")
        out.append(f"=== {label}: {page['title']} ===")
        for op in page["ops"]:
            words = re.sub(r"\s+", " ", page_text(op)).strip()
            out.append(f"[{op['id']}] {words}")
    return "\n".join(out)
