"""The board scribe — turns what the teacher SAYS into what the board SHOWS.

Why this exists
---------------
The classroom used to run two minds: the live voice talked, and a separate model
guessed what the board should say. Everything that went wrong — the board not
matching her words, repeated lines, her announcing a write that never appeared —
came from that one split.

Here there is no guessing. Every sentence she speaks is classified, and the board
is written from her own words. The board therefore cannot disagree with her.

Speed
-----
Classification uses OpenAI's Decisions API (a bounded multiple-choice call,
~350-400ms) and runs alongside her speech, so it never delays the voice. Only
``formula`` lines need a generation call, to turn spoken maths ("two H two plus
O two") into real notation ($2\\text{H}_2 + \\text{O}_2$). Everything else is
written straight from the transcript with no model call at all.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Literal

import httpx

from app.config import settings
from app.core import llm

logger = logging.getLogger("studymaster.board_scribe")

OPENAI_BASE = "https://api.openai.com/v1"

Verdict = Literal["skip", "point", "key", "formula", "working"]

CHOICES = [
    ("skip", "Greeting, encouragement, praise, a question put to the student, "
             "chit-chat, or a rephrasing of something already said. Nothing to write."),
    ("point", "A teaching statement worth writing down as a plain line of notes."),
    ("key", "The crucial rule, definition or idea the student must remember."),
    ("formula", "Contains a formula, equation, chemical equation or symbolic maths "
                "that must be written in proper notation."),
    ("working", "One step of a calculation or worked solution."),
]

# Spoken maths almost always contains these; used only as a cheap pre-filter and
# as the fallback when the Decisions call is unavailable.
_MATHY = re.compile(
    r"\b(equals?|plus|minus|times|divided|over|squared|cubed|root|log|sine|cosine|"
    r"tangent|power|fraction|percent|ratio|gives|yields|arrow)\b",
    re.IGNORECASE,
)


def _fallback_verdict(sentence: str) -> Verdict:
    """Deterministic classification for when Decisions is unreachable."""
    text = sentence.strip()
    if not text:
        return "skip"
    lowered = text.lower()
    if text.endswith("?"):
        return "skip"
    if len(text.split()) <= 4:
        return "skip"
    if any(lowered.startswith(w) for w in (
        "hello", "hi ", "welcome", "well done", "good", "exactly", "correct",
        "nice", "okay", "ok ", "right,", "does that", "do you", "shall we",
    )):
        return "skip"
    if _MATHY.search(text):
        return "formula"
    if any(w in lowered for w in ("remember", "always", "never", "the rule is", "key thing")):
        return "key"
    return "point"


async def classify(sentence: str, *, subject: str, topic: str) -> Verdict:
    """Decide what, if anything, this spoken sentence becomes on the board."""
    text = (sentence or "").strip()
    if len(text) < 3:
        return "skip"
    if not settings.openai_api_key:
        return _fallback_verdict(text)

    payload = {
        "model": settings.openai_decisions_model,
        "input": (
            f"A teacher is giving a live {subject} lesson on {topic}. "
            f"She just said this out loud:\n\n\"{text}\"\n\n"
            "Decide what should happen to it on the teaching board."
        ),
        "questions": [{
            "type": "choice",
            "name": "board_action",
            "instructions": (
                "Classify the sentence. Anything that is just talk — greeting, praise, "
                "a question to the student, or repeating what was already said — is 'skip'."
            ),
            "choices": [{"value": v, "description": d} for v, d in CHOICES],
        }],
    }
    try:
        async with httpx.AsyncClient(timeout=6) as client:
            response = await client.post(
                f"{OPENAI_BASE}/decisions",
                headers={"Authorization": f"Bearer {settings.openai_api_key}"},
                json=payload,
            )
        response.raise_for_status()
        for answer in response.json().get("answers", []):
            if answer.get("name") == "board_action" and answer.get("choice"):
                return answer["choice"]  # type: ignore[return-value]
    except Exception as exc:  # noqa: BLE001 — never let the board stall the lesson
        logger.info("Decisions unavailable (%s); using the local rule", str(exc)[:120])
    return _fallback_verdict(text)


async def to_notation(sentence: str, *, subject: str) -> dict[str, str]:
    """Turn spoken maths into real notation.

    "two H two plus O two gives two H two O"
        -> {"latex": "2\\text{H}_2 + \\text{O}_2 \\rightarrow 2\\text{H}_2\\text{O}",
            "text": "..."}

    Returns an empty latex field if there is nothing symbolic to render, in which
    case the caller simply writes the sentence as prose.
    """
    prompt = f"""A {subject} teacher said this out loud during a live lesson:

"{sentence}"

Write the mathematics or chemistry she just spoke, in LaTeX, exactly as it would appear
on a board. Do not solve anything, do not add steps, do not explain — only transcribe
the notation she actually said.

Rules:
- Chemical formulas use \\text{{}} and subscripts: 2\\text{{H}}_2 + \\text{{O}}_2 \\rightarrow 2\\text{{H}}_2\\text{{O}}
- Reactions use \\rightarrow; equations use =
- Spoken maths becomes real notation: "log base ten of one thousand equals three"
  -> \\log_{{10}}(1000) = 3 ;  "v squared equals u squared plus two a s" -> v^2 = u^2 + 2as
- NEVER wrap an ordinary English sentence in \\text{{}}. If what she said is an explanation in
  words rather than an equation, return an empty string for "latex". A sentence like
  "a logarithm asks what power you raise the base to" has NO notation — return empty.
- Only return latex when there is a real equation, formula or expression to typeset.

Return strict JSON: {{"latex": "...", "caption": "a short label, 6 words max, or empty"}}"""
    try:
        data = await llm.chat_json(
            [{"role": "system", "content": "You transcribe spoken mathematics into LaTeX. Nothing else."},
             {"role": "user", "content": prompt}],
            tier="default", temperature=0.0, max_tokens=300,
        )
        latex = str(data.get("latex", "")).strip()
        caption = re.sub(r"\s+", " ", str(data.get("caption", ""))).strip()

        # Models sometimes "convert" an English sentence by wrapping the whole
        # thing in \text{...}. That is prose with extra steps — reject it so the
        # line is written as ordinary words instead of fake maths.
        bare = re.sub(r"^\\text\{(.*)\}$", r"\1", latex, flags=re.DOTALL).strip()
        if bare != latex and not re.search(r"[=+\-^_\\]|\\d", bare.replace("\\text", "")):
            latex = ""
        elif latex and not re.search(r"[=+\-^_(){}\\]|\\d", latex):
            latex = ""
        # a "formula" that is mostly words is not a formula
        if latex:
            words = len(re.findall(r"[A-Za-z]{4,}", re.sub(r"\\[a-zA-Z]+", "", latex)))
            if words >= 6:
                latex = ""
        return {"latex": latex, "caption": caption[:60]}
    except Exception:  # noqa: BLE001
        logger.exception("notation transcription failed")
        return {"latex": "", "caption": ""}


def tidy(sentence: str) -> str:
    """Light clean-up of speech for the board: drop fillers, fix spacing.

    Deliberately conservative — these are HER words and must stay recognisable.
    """
    text = re.sub(r"\s+", " ", (sentence or "").strip())
    text = re.sub(r"^(so|okay|ok|now|right|well|and|but|um|uh|erm)[,\s]+", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\b(um|uh|erm|you know|i mean)\b[,\s]*", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\s+([,.;:?!])", r"\1", text)
    text = re.sub(r"\s{2,}", " ", text).strip(" ,;-")
    if text and text[0].islower():
        text = text[0].upper() + text[1:]
    return text


async def build_roadmap(*, subject: str, topic: str, level: str,
                        struggle: str) -> list[dict[str, Any]]:
    """Turn the opening interview into an agreed plan for the lesson."""
    prompt = f"""A Nigerian secondary-school student is in a live one-to-one {subject} lesson on "{topic}".
They describe their level as: {level}.
When asked what they are stuck on, they said: "{struggle or 'nothing specific — they just want to learn the topic'}"

Plan the lesson as 3 to 5 steps that build on each other, starting from whatever they are
actually missing — not from the beginning of the textbook unless that is where the gap is.

Each step is something teachable in 5-10 minutes and checkable with one question.
Titles are short and plain, as a student would say them — "What a logarithm really means",
not "Introduction to logarithmic functions".

Return strict JSON: {{"steps":[{{"title":"...","detail":"one line on what they will be able to do"}}]}}"""
    try:
        data = await llm.chat_json(
            [{"role": "system", "content": "You plan focused one-to-one lessons."},
             {"role": "user", "content": prompt}],
            tier="default", temperature=0.3, max_tokens=600,
        )
        steps = []
        for item in (data.get("steps") or [])[:5]:
            title = re.sub(r"\s+", " ", str(item.get("title", ""))).strip()
            if title:
                steps.append({
                    "title": title[:80],
                    "detail": re.sub(r"\s+", " ", str(item.get("detail", ""))).strip()[:160],
                    "status": "pending",
                })
        return steps
    except Exception:  # noqa: BLE001
        logger.exception("roadmap generation failed")
        return []


# She says this out loud when a step is finished; it is what wipes the board.
_ADVANCES = re.compile(
    r"(move|moving|go|going|on)\s+(on|ahead)?\s*to\s+the\s+next"
    r"|next\s+step"
    r"|let'?s\s+move\s+on"
    r"|that'?s\s+(that|step)\s+(one|two|three|four|five)?\s*(done|finished|covered)",
    re.IGNORECASE,
)


def announces_next_step(text: str) -> bool:
    return bool(_ADVANCES.search(text or ""))


async def scribe(sentence: str, *, subject: str, topic: str) -> dict[str, Any]:
    """One spoken sentence in, one board line out (or nothing).

    This is the whole pipeline: classify, convert notation when there is any,
    and hand back something written from HER words. Never invents content.
    """
    text = tidy(sentence)
    if not text:
        return {"verdict": "skip"}

    verdict = await classify(text, subject=subject, topic=topic)
    if verdict == "skip":
        return {"verdict": "skip"}

    # Maths can hide inside a line the classifier called "key" or "working", so
    # notation is attempted whenever the sentence sounds symbolic at all.
    latex = ""
    caption = ""
    if verdict == "formula" or _MATHY.search(text):
        notation = await to_notation(text, subject=subject)
        latex = notation.get("latex", "")
        caption = notation.get("caption", "")

    if latex:
        return {
            "verdict": verdict,
            "action": "write_formula",
            "content": {"label": caption, "latex": latex, "explanation": ""},
            "note": {"text": text, "kind": "formula", "latex": latex},
            "emphasis": "key" if verdict == "key" else "normal",
        }

    action = "write_text"
    note_kind = "key" if verdict == "key" else ("working" if verdict == "working" else "point")
    return {
        "verdict": verdict,
        "action": action,
        "content": {"text": text, "emphasis": "key" if verdict == "key" else "normal"},
        "note": {"text": text, "kind": note_kind, "latex": ""},
        "emphasis": "key" if verdict == "key" else "normal",
    }
