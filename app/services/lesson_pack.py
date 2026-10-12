"""The lesson pack — board content prepared BEFORE the teacher needs it.

Why this exists
---------------
A live teacher says "let me put this on the board" and it appears. Ours took ~5s
because the board was only composed at that moment: the reasoning model thought,
then generated every line. The fix used by voice tutors with a shared board is
to prepare the visuals ahead of speech. While the teacher is still greeting the
student and asking what they are stuck on (~15-20s of conversation), this module
plans the lesson and writes every step's board content — definitions, key lists,
a worked example, a check quiz, a real Wikimedia picture.

At teaching time the model only picks a card by id (``show_card("s2c1")``): a
tiny tool call that renders instantly because the content already exists on the
student's screen.

Flow
----
1. ``start_prep`` (at session creation) — background: the plan draft (~2s), then
   each step's cards as its own task (~10-20s, gpt-6-luna for accuracy).
2. The browser polls ``/pack?ready=n`` and re-briefs the board assistant as each
   step's cards land — usually before the student has even answered.
3. ``adapt`` (once the student says what they struggle with) — one tiny call
   re-orders the prepared steps; cards are reused, so the plan shows in ~1s.
Card ids (``s2c1``) are fixed when written, so re-ordering never changes them.
"""

from __future__ import annotations

import asyncio
import logging
import re
from typing import Any

from app.config import settings
from app.core import llm
from app.services import wikimedia

logger = logging.getLogger("studymaster.lesson_pack")

# lessonId -> its preparation. In-process is enough: a lesson lives on the
# worker that created it, and a missing entry just means we prepare on demand.
_PREP: dict[str, _Prep] = {}


_JSON_ESCAPES = {"\f": "\\f", "\b": "\\b", "\x0b": "\\v", "\x07": "\\a"}


def _fix_latex(text: str) -> str:
    r"""Undo LaTeX-in-JSON damage BEFORE whitespace is collapsed.

    "\frac" can arrive as form-feed + "rac", "\times" as TAB + "imes",
    "\neq" as NEWLINE + "eq"; other stray control characters (we saw
    "$\x02\log_2 8$") make KaTeX draw red error text.
    """
    for bad, good in _JSON_ESCAPES.items():
        text = text.replace(bad, good)
    text = re.sub(r"\t(?=[a-z])", r"\\t", text)
    text = re.sub(r"\r(?=[a-z])", r"\\r", text)
    text = re.sub(r"\n(?=(?:e|eq|eg|abla|ot|u|i)\b|(?:eq|abla|eg|ot)[^a-z])", r"\\n", text)
    text = re.sub(r"[\x00-\x08\x0e-\x1f]", "", text)
    if len(re.findall(r"(?<!\\)\$", text)) % 2:  # close an unbalanced "$N>0."
        text = re.sub(r"([.,;:!?]*)\s*$", r"$\1", text, count=1)
    return text


def _clean(value: Any, limit: int = 400) -> str:
    text = re.sub(r"\s+", " ", _fix_latex(str(value or ""))).strip()
    # the board renders maths, not markdown: *E. coli* / **key** would show their stars
    text = re.sub(r"\*{1,2}([^*$]+?)\*{1,2}", r"\1", text)
    return text[:limit]


async def _draft_steps(*, subject: str, topic: str, level: str, details: str) -> list[dict[str, str]]:
    prompt = f"""Plan a live one-to-one {subject} lesson on "{topic}" for a Nigerian WAEC/JAMB student.
Their level: {level}. What they told us: {details or 'nothing extra'}.

Give 4 steps that build on each other and together cover what WAEC/JAMB examine on this
topic. Each step is teachable in 5-8 minutes and checkable with one question. Titles are
short and plain, as a student would say them ("What makes something alive"), not
textbook headings.

Return strict JSON: {{"steps":[{{"title":"...","detail":"one line on what they will be able to do"}}]}}"""
    data = await llm.chat_json(
        [{"role": "system", "content": "You plan focused one-to-one lessons."},
         {"role": "user", "content": prompt}],
        tier="default", temperature=0.3, max_tokens=500,
    )
    steps = []
    for item in (data.get("steps") or [])[:5]:
        title = _clean(item.get("title"), 80)
        if title:
            steps.append({"title": title, "detail": _clean(item.get("detail"), 160)})
    return steps


_CARD_PROMPT = """You are preparing the WHITEBOARD for one step of a live {subject} lesson on "{topic}"
for a Nigerian WAEC/JAMB student (level: {level}).
This step: "{title}" — {detail}
Other steps in the lesson (do NOT cover them here): {others}

Write the board content a good teacher would put up while teaching this step, as 3-5 cards
in teaching order. Card kinds:
- "definition": {{"kind":"definition","heading":"2-5 words","lines":["the exact definition", "one short example of it"]}}
- "list":       {{"kind":"list","heading":"2-5 words","lines":["short item", "..."]}}   (3-8 items, e.g. features, stages, types)
- "formula":    {{"kind":"formula","heading":"2-5 words","latex":"LaTeX with NO dollar signs","lines":["what each symbol means"]}}
- "example":    {{"kind":"example","heading":"2-5 words","problem":"a WAEC-style example or question","solution":["step 1","step 2","..."]}}
- "quiz":       {{"kind":"quiz","question":"...","options":["A","B","C","D"],"correctIndex":0,"explanations":["why each option is right / what mistake it is"]}}

Rules:
- Board lines are SHORT — a board is not a paragraph. No line over ~90 characters.
- Maths/chemistry inline in $...$ (e.g. $\\text{{H}}_2\\text{{O}}$, $v = u + at$). Use "formula" only when the
  topic genuinely has one; never invent formulas for descriptive topics.
- End with exactly ONE "quiz" card that checks this step. Wrong options are mistakes real students make.
- Verify every fact and calculation. Never use an example that is itself a classic misconception
  (e.g. "breathing" as an example of respiration) — those belong in quiz wrong options instead. Use Nigerian everyday examples where natural (yam, goat, okada...).
- Do NOT re-define the lesson topic itself or repeat content belonging to the other steps — this
  step's cards cover ONLY this step.
- "imageQuery": the name of ONE concrete thing whose Wikipedia article picture would genuinely help
  (e.g. "Amoeba", "Plant cell", "Hibiscus", "Mitosis", "Simple pendulum"). Empty string if a picture
  would not help (e.g. algebra, grammar).

Return strict JSON: {{"cards":[...], "imageQuery":"..."}}"""


def _card_ops(card: dict[str, Any]) -> list[dict[str, Any]]:
    """Turn one prepared card into the board operations the board already renders."""
    kind = card.get("kind")
    heading = _clean(card.get("heading"), 80)
    lines = [_clean(x, 220) for x in (card.get("lines") or []) if _clean(x)][:8]
    ops: list[dict[str, Any]] = []
    if kind == "quiz":
        options = [_clean(o, 200) for o in (card.get("options") or []) if _clean(o)][:4]
        if len(options) < 2:
            return []
        try:
            correct = int(card.get("correctIndex", 0))
        except (TypeError, ValueError):
            correct = 0
        return [{"action": "show_question", "emphasis": "normal", "content": {
            "question": _clean(card.get("question"), 400),
            "options": options,
            "correctIndex": min(max(correct, 0), len(options) - 1),
            "explanations": [_clean(e, 240) for e in (card.get("explanations") or [])][:4],
        }}]
    if kind == "example":
        solution = [_clean(x, 220) for x in (card.get("solution") or []) if _clean(x)][:8]
        if not card.get("problem"):
            return []
        return [{"action": "show_example", "emphasis": "normal", "content": {
            "title": heading, "problem": _clean(card.get("problem"), 400), "solution": solution,
        }}]
    if heading:
        ops.append({"action": "write_heading", "emphasis": "normal", "content": {"text": heading}})
    if kind == "formula" and card.get("latex"):
        ops.append({"action": "write_formula", "emphasis": "key", "content": {
            "label": "", "latex": str(card.get("latex")).strip().strip("$"), "explanation": "",
        }})
    emphasis = "key" if kind == "definition" else "normal"
    for i, line in enumerate(lines):
        # the first line of a definition is the thing to remember
        ops.append({"action": "write_text", "emphasis": emphasis if i == 0 else "normal",
                    "content": {"text": line, "emphasis": emphasis if i == 0 else "normal"}})
    return ops


def _preview(card: dict[str, Any]) -> str:
    """A compact read of a card so the models know exactly what it says."""
    parts: list[str] = []
    for op in card["ops"]:
        c = op["content"]
        if op["action"] == "show_question":
            parts.append(f"Q: {c.get('question', '')} options: {' | '.join(c.get('options', []))}")
        elif op["action"] == "show_example":
            parts.append(f"{c.get('problem', '')} -> {' ; '.join(c.get('solution', [])[:3])}")
        elif op["action"] == "show_image":
            parts.append(f"picture: {c.get('caption', '')}")
        elif op["action"] == "write_formula":
            parts.append(c.get("latex", ""))
        elif op["action"] == "write_text":
            parts.append(c.get("text", ""))
    return _clean("; ".join(p for p in parts if p), 260)


async def pick_image(query: str, *, purpose: str) -> dict[str, Any] | None:
    """Find ONE picture that genuinely shows ``purpose``, or nothing.

    Wikipedia lead images are the main source (editors chose them to show that
    exact subject); Commons fills gaps. A tiny model call then rejects
    near-misses — a wrong picture on a board is worse than none.
    """
    if not query:
        return None
    # The plain concept ("bacterium" from "bacterium microscope") finds the
    # main Wikipedia article, whose lead image is the typical textbook picture.
    concept = re.sub(r"\b(microscope|microscopic|diagram|picture|photo|image|labelled|labeled|under)\b", "", query, flags=re.IGNORECASE).strip()
    searches = [wikimedia.search_wikipedia_images(query, limit=4), wikimedia.search_images(query, limit=3, width=800)]
    if concept and concept.lower() != query.lower():
        searches.insert(0, wikimedia.search_wikipedia_images(concept, limit=2))
    found = await asyncio.gather(*searches)
    seen: set[str] = set()
    candidates = [c for group in found for c in group if not (c["url"] in seen or seen.add(c["url"]))][:8]
    if not candidates:
        return None
    listing = "\n".join(
        f"{i}. [{'lead picture of the encyclopedia article' if c.get('license') == 'Wikipedia' else 'photo library'}] "
        f"{c['title']} — {_clean(c.get('description'), 140)}"
        for i, c in enumerate(candidates))
    try:
        data = await llm.chat_json(
            [{"role": "system", "content": "You choose teaching pictures. Reply with JSON only."},
             {"role": "user", "content": (
                 f"A teacher wants a picture showing: {purpose} (search: {query}).\n"
                 f"Candidates:\n{listing}\n\nPick the one a secondary-school student would immediately "
                 "recognise as showing that — the clear, TYPICAL, textbook-style picture of the general "
                 "thing, not a rare species or a niche research specimen. Reject portraits of people, "
                 "logos, maps, and charts of unrelated things. The lead picture of the encyclopedia "
                 "article about the thing itself (e.g. the article 'Bacteria' for a bacterium) is "
                 "usually the safest choice. "
                 'Return {"index": n} or {"index": -1} if none truly fits.')}],
            tier="default", temperature=0.0, max_tokens=20,
        )
        index = int(data.get("index", -1))
    except Exception:
        logger.warning("image pick failed for %r", query, exc_info=True)
        return None
    if not 0 <= index < len(candidates):
        return None
    best = candidates[index]
    return {
        "url": best["url"], "caption": _clean(best.get("description") or best["title"], 160),
        "title": best["title"], "source": best.get("pageUrl", ""), "license": best.get("license", ""),
        "query": query,
    }


async def _image_card(query: str, purpose: str) -> dict[str, Any] | None:
    picture = await pick_image(query, purpose=purpose)
    return {"action": "show_image", "emphasis": "normal", "content": picture} if picture else None


async def build_step_cards(*, subject: str, topic: str, level: str, step: dict[str, str],
                           others: list[str], step_no: int) -> list[dict[str, Any]]:
    """All board cards for one step, plus its picture. Never raises."""
    try:
        data = await llm.chat_json(
            [{"role": "system", "content": "You prepare accurate whiteboard content for live lessons."},
             {"role": "user", "content": _CARD_PROMPT.format(
                 subject=subject, topic=topic, level=level, title=step["title"],
                 detail=step.get("detail", ""), others="; ".join(others) or "none")}],
            tier="smart", model=settings.lesson_pack_model, temperature=1,
            reasoning_effort="low", max_completion_tokens=8000,
        )
    except Exception:
        logger.exception("card generation failed for step %r", step.get("title"))
        return []
    raw_cards = [c for c in (data.get("cards") or []) if isinstance(c, dict)]
    cards: list[dict[str, Any]] = []
    for raw in raw_cards[:6]:
        ops = _card_ops(raw)
        if ops:
            title = raw.get("heading") or (f"check question: {raw.get('question')}" if raw.get("kind") == "quiz" else raw.get("kind"))
            cards.append({"kind": raw.get("kind", "list"), "title": _clean(title, 70), "ops": ops})
    image = await _image_card(_clean(data.get("imageQuery"), 60), f"{step['title']} ({topic})")
    if image:
        # the picture goes right after the first card, where a teacher would show it
        cards.insert(min(1, len(cards)), {"kind": "image", "title": image["content"]["title"][:60], "ops": [image]})
    for i, card in enumerate(cards):
        card["id"] = f"s{step_no}c{i + 1}"
        card["preview"] = _preview(card)
    return cards


class _Prep:
    """One lesson's preparation, in two stages so the plan never waits on cards.

    ``steps`` (the plan draft, ~2s) finishes long before the student has said
    what they struggle with. Each step's cards are their own task (~10-20s) and
    are attached to the plan as they land.
    """

    def __init__(self, **kwargs: Any) -> None:
        self.kwargs = kwargs
        self.cards: dict[str, asyncio.Task] = {}
        self.steps = asyncio.create_task(self._draft())

    async def _draft(self) -> list[dict[str, str]]:
        k = self.kwargs
        steps = await _draft_steps(subject=k["subject"], topic=k["topic"],
                                   level=k["level"], details=k["details"])
        self.ensure_cards(steps)
        return steps

    def ensure_cards(self, steps: list[dict[str, Any]]) -> None:
        titles = [s["title"] for s in steps]
        for i, step in enumerate(steps):
            if step["title"] not in self.cards:
                self.cards[step["title"]] = asyncio.create_task(build_step_cards(
                    subject=self.kwargs["subject"], topic=self.kwargs["topic"],
                    level=self.kwargs["level"], step=step,
                    others=[t for t in titles if t != step["title"]], step_no=i + 1,
                ))


def start_prep(lesson_id: str, **kwargs: Any) -> None:
    """Kick off preparation in the background; the lesson keeps talking meanwhile."""
    if lesson_id not in _PREP:
        _PREP[lesson_id] = _Prep(**kwargs)


async def get_steps(lesson_id: str, *, timeout: float = 15.0) -> list[dict[str, Any]] | None:
    prep = _PREP.get(lesson_id)
    if prep is None:
        return None
    try:
        steps = await asyncio.wait_for(asyncio.shield(prep.steps), timeout=timeout)
        return [dict(s) for s in steps]
    except Exception:
        logger.warning("lesson plan draft for %s not usable", lesson_id, exc_info=True)
        return None


def ready_cards(lesson_id: str, title: str) -> list[dict[str, Any]] | None:
    """Cards for one step if they are already written; never waits."""
    prep = _PREP.get(lesson_id)
    task = prep.cards.get(title) if prep else None
    if task is None or not task.done() or task.cancelled() or task.exception():
        return None
    return task.result()


async def wait_cards(lesson_id: str, steps: list[dict[str, Any]], *, subject: str, topic: str,
                     level: str, timeout: float = 45.0, at_least: int | None = None) -> None:
    """Fill in steps' cards, waiting for the ones still being written.

    ``at_least`` returns as soon as that many steps have cards (so the board
    assistant can be briefed step by step instead of after the slowest one).
    Also covers a lost preparation (server restart): cards are then written now.
    """
    prep = _PREP.get(lesson_id)
    if prep is None:
        prep = _PREP[lesson_id] = _Prep.__new__(_Prep)
        prep.kwargs = {"subject": subject, "topic": topic, "level": level, "details": ""}
        prep.cards = {}
    prep.ensure_cards(steps)
    tasks = [prep.cards[s["title"]] for s in steps]
    want = len(tasks) if at_least is None else min(at_least, len(tasks))
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while sum(t.done() for t in tasks) < want and loop.time() < deadline:
        pending = [t for t in tasks if not t.done()]
        await asyncio.wait(pending, timeout=deadline - loop.time(), return_when=asyncio.FIRST_COMPLETED)
    for step, task in zip(steps, tasks, strict=True):
        if task.done() and not task.cancelled() and not task.exception():
            step["cards"] = task.result()


def prepared_now(lesson_id: str) -> list[dict[str, Any]] | None:
    """Prepared steps with whatever cards are written so far; never waits."""
    prep = _PREP.get(lesson_id)
    if prep is None or not prep.steps.done() or prep.steps.cancelled() or prep.steps.exception():
        return None
    steps = [dict(s) for s in prep.steps.result()]
    for step in steps:
        step["cards"] = ready_cards(lesson_id, step["title"]) or []
    return steps


def forget(lesson_id: str) -> None:
    _PREP.pop(lesson_id, None)


async def adapt(*, steps: list[dict[str, Any]], subject: str, topic: str, struggle: str) -> list[dict[str, Any]]:
    """Re-shape the prepared steps around what the student said they struggle with.

    Only picks and orders existing steps (so their board content is reused and
    instant). Fast: small model, tiny output.
    """
    if not steps:
        return []
    listing = "\n".join(f"{i}. {s['title']} — {s.get('detail', '')}" for i, s in enumerate(steps))
    prompt = f"""A student in a live {subject} lesson on "{topic}" was asked what they struggle with. They said:
"{struggle}"

Prepared steps:
{listing}

Choose the order to teach the prepared steps, starting where their gap actually is. Keep EVERY
step unless the student explicitly said they already know it — a vague answer, "everything" or a
specific difficulty is NOT a reason to drop steps; it is a reason to put that step first. Return strict JSON:
{{"order":[indices]}}"""
    try:
        data = await llm.chat_json(
            [{"role": "system", "content": "You adapt lesson plans. Reply with JSON only."},
             {"role": "user", "content": prompt}],
            tier="default", temperature=0.0, max_tokens=60,
        )
        order = [int(i) for i in data.get("order", []) if str(i).lstrip("-").isdigit()]
        order = [i for i in dict.fromkeys(order) if 0 <= i < len(steps)]
    except Exception:
        logger.warning("plan adapt failed; keeping prepared order", exc_info=True)
        order = []
    return [steps[i] for i in (order or range(len(steps)))]


def step_menu(step: dict[str, Any]) -> str:
    """What is prepared for one step, in words the VOICE can plan around
    ("there is a picture and a quiz ready")."""
    items = []
    for card in step.get("cards") or []:
        label = "picture" if card["kind"] == "image" else card["kind"]
        items.append(f"{label}: {card['title']}")
    return "; ".join(items)


def catalog(steps: list[dict[str, Any]], *, agreed: bool = True) -> str:
    """The readable index of prepared cards, for the models' instructions.

    Card ids are fixed when the cards are written (s<prepared step>c<n>), so a
    later re-ordering of the plan never changes what an id points at.
    """
    out = [] if agreed else ["(Plan not agreed with the student yet — steps may be re-ordered.)"]
    for n, step in enumerate(steps, start=1):
        out.append(f"STEP {n}: {step['title']}"
                   + ("" if step.get("cards") else "  (cards still being written — use write_on_board)"))
        for card in step.get("cards") or []:
            out.append(f"  {card['id']} [{card['kind']}] {card['title']} — {card.get('preview', '')}")
    return "\n".join(out)
