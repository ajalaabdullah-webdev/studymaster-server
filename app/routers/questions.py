"""Past-questions endpoints — ported from Node, upgraded for exam-first filtering.

    GET  /api/v1/questions            list/filter (subject, year, exam_name, question_type, limit, page)
    GET  /api/v1/questions/filters    distinct filter options; accepts exam_name and/or subject
    POST /api/v1/questions/explain    AI explanation of one question
    POST /api/v1/questions/follow-up  follow-up chat on an explanation
    POST /api/v1/questions/generate-answer  model answer for a theory question
"""

import json
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sse_starlette.sse import EventSourceResponse

from app.core.deps import get_current_user
from app.core.llm import chat, chat_stream
from app.models.question import Question
from app.models.user import User
from app.services.questions import ACTIVE_EXAMS

router = APIRouter(prefix="/api/v1/questions", tags=["questions"])


class ExplainRequest(BaseModel):
    questionData: dict[str, Any]
    selected_answer: Optional[str] = None
    onboardingData: Optional[dict[str, Any]] = None


class FollowUpRequest(BaseModel):
    message: str
    history: Optional[list[dict[str, Any]]] = None
    questionData: Optional[dict[str, Any]] = None
    onboardingData: Optional[dict[str, Any]] = None


class GenerateAnswerRequest(BaseModel):
    questionData: dict[str, Any]


@router.get("")
async def get_questions(
    subject: Optional[str] = None,
    year: Optional[int] = None,
    exam_name: Optional[str] = None,
    question_type: Optional[str] = None,
    limit: Optional[int] = Query(default=None, le=500),
    page: int = 1,
) -> dict:
    query: dict[str, Any] = {}
    if subject:
        query["subject"] = subject.lower()
    if year:
        query["year"] = year
    if exam_name:
        query["exam_name"] = exam_name.upper()
    else:
        query["exam_name"] = {"$in": ACTIVE_EXAMS}  # platform focus: WAEC + JAMB
    if question_type:
        query["question_type"] = question_type

    find = Question.find(query).sort(-Question.year)
    total = await Question.find(query).count()
    if limit:
        find = find.skip((page - 1) * limit).limit(limit)
    questions = await find.to_list()

    return {
        "success": True,
        "count": len(questions),
        "pagination": {"total": total, "page": page, "pages": (total + limit - 1) // limit if limit else 1},
        "data": [{**q.model_dump(mode="json"), "_id": str(q.id)} for q in questions],
    }


@router.get("/search")
async def search_topic(
    subject: str,
    topic: str,
    exam_name: Optional[str] = None,
    question_type: str = "obj",
    limit: int = Query(default=20, le=60),
    current: User = Depends(get_current_user),
) -> dict:
    """Topic practice: 'number bases' -> matching questions. Hybrid search —
    labels + text, semantic (vector) neighbours, and a cached keyword expansion.
    See services/topic_search for why all three layers are needed."""
    from app.services.topic_search import search_by_topic

    data = await search_by_topic(
        subject=subject, topic=topic, exam_name=exam_name,
        question_type=question_type, limit=limit,
    )
    return {"success": True, "count": len(data), "topic": topic, "data": data}


@router.get("/topics")
async def list_subject_topics(
    subject: str,
    exam_name: Optional[str] = None,
    limit: int = Query(default=60, le=200),
    current: User = Depends(get_current_user),
) -> dict:
    """Topics that actually exist for a subject, most-covered first — so the UI
    can offer real topics instead of making students guess what to type."""
    from app.services.topic_search import list_topics

    data = await list_topics(subject=subject, exam_name=exam_name, limit=limit)
    return {"success": True, "count": len(data), "data": data}


@router.get("/filters")
async def get_filter_options(
    subject: Optional[str] = None, exam_name: Optional[str] = None
) -> dict:
    """Exam-first filtering: pass exam_name to see only that exam's subjects/years."""
    query: dict[str, Any] = {}
    if exam_name:
        query["exam_name"] = exam_name.upper()
    else:
        query["exam_name"] = {"$in": ACTIVE_EXAMS}
    if subject:
        query["subject"] = subject.lower()

    subjects = sorted(await Question.distinct("subject", {"exam_name": query["exam_name"]}))
    years = sorted([y for y in await Question.distinct("year", query) if y], reverse=True)
    exam_names = sorted(await Question.distinct("exam_name", {"exam_name": {"$in": ACTIVE_EXAMS}}))
    question_types = sorted(await Question.distinct("question_type", query))

    return {
        "success": True,
        "data": {"subjects": subjects, "years": years, "examNames": exam_names, "questionTypes": question_types},
    }


def _explain_messages(body: ExplainRequest) -> list[dict[str, Any]]:
    q = body.questionData
    user_class = (body.onboardingData or {}).get("userClass") or "WAEC/JAMB"
    is_obj = q.get("question_type") == "obj"
    prompt = f"""You are a warm, expert Nigerian tutor. Explain this {q.get('exam_name', '')} {q.get('subject', '')}
question to a {user_class} student, simply and clearly.

QUESTION: "{q.get('question_text', '')}"
{f"OPTIONS: {q.get('options')}" if is_obj else ""}
CORRECT ANSWER: "{q.get('correct_answer', '')}"
{f'STUDENT ANSWER: "{body.selected_answer}"' if body.selected_answer else ""}

RULES:
1. Markdown headers (###) for sections, **bold** for emphasis.
2. LaTeX for ALL maths, chemistry and physics notation — including single symbols,
   numbers with units, equations, fractions, indices and ions. ALWAYS delimit it:
   $...$ inline, $$...$$ on its own line. NEVER write \\( \\) or \\[ \\], and never
   leave a LaTeX command such as \\frac or \\sqrt outside $...$.
3. Short sentences, simple words; explain hard terms in brackets.
4. Focus on the "why" so the student truly learns the concept.
5. Friendly and encouraging."""
    return [
        {"role": "system", "content": "You are a specialized academic tutor who writes clean Markdown and LaTeX."},
        {"role": "user", "content": prompt},
    ]


@router.post("/explain-stream")
async def explain_stream(body: ExplainRequest, current: User = Depends(get_current_user)):
    """Streaming explanation — text starts arriving immediately (SSE deltas)."""
    if not body.questionData:
        raise HTTPException(status_code=400, detail="Question data is required")
    messages = _explain_messages(body)

    async def gen():
        try:
            async for delta in chat_stream(messages, tier="default", temperature=0.5):
                yield {"data": json.dumps({"delta": delta})}
            yield {"data": json.dumps({"done": True})}
        except Exception as exc:  # noqa: BLE001
            yield {"data": json.dumps({"error": str(exc)[:200]})}

    return EventSourceResponse(gen())


@router.post("/follow-up-stream")
async def follow_up_stream(body: FollowUpRequest, current: User = Depends(get_current_user)):
    if not body.message:
        raise HTTPException(status_code=400, detail="Message is required")
    user_class = (body.onboardingData or {}).get("userClass") or "WAEC/JAMB"
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": f"You are a warm, patient Nigerian tutor helping a {user_class} student. Use clean Markdown and LaTeX ($...$)."}
    ]
    for m in (body.history or [])[-10:]:
        if m.get("role") in {"user", "assistant"} and m.get("content"):
            messages.append({"role": m["role"], "content": m["content"]})
    q = body.questionData or {}
    ctx = f'ORIGINAL QUESTION: "{q.get("question_text", "")}"\nCORRECT ANSWER: "{q.get("correct_answer", "")}"\n\n' if q else ""
    messages.append({"role": "user", "content": f"{ctx}STUDENT'S FOLLOW-UP: \"{body.message}\"\n\nAnswer their specific concern clearly and simply. LaTeX for all maths."})

    async def gen():
        try:
            async for delta in chat_stream(messages, tier="default", temperature=0.5):
                yield {"data": json.dumps({"delta": delta})}
            yield {"data": json.dumps({"done": True})}
        except Exception as exc:  # noqa: BLE001
            yield {"data": json.dumps({"error": str(exc)[:200]})}

    return EventSourceResponse(gen())


@router.post("/explain")
async def explain_question(body: ExplainRequest, current: User = Depends(get_current_user)) -> dict:
    q = body.questionData
    if not q:
        raise HTTPException(status_code=400, detail="Question data is required")
    user_class = (body.onboardingData or {}).get("userClass") or "WAEC/JAMB"
    is_obj = q.get("question_type") == "obj"

    prompt = f"""You are a warm, expert Nigerian tutor. Explain this {q.get('exam_name', '')} {q.get('subject', '')}
question to a {user_class} student, simply and clearly.

QUESTION: "{q.get('question_text', '')}"
{f"OPTIONS: {q.get('options')}" if is_obj else ""}
CORRECT ANSWER: "{q.get('correct_answer', '')}"
{f"STUDENT'S ANSWER: \"{body.selected_answer}\"" if body.selected_answer else ""}

RULES:
1. Markdown headers (###) for sections, **bold** for emphasis.
2. LaTeX for ALL maths, chemistry and physics notation — including single symbols,
   numbers with units, equations, fractions, indices and ions. ALWAYS delimit it:
   $...$ inline, $$...$$ on its own line. NEVER write \\( \\) or \\[ \\], and never
   leave a LaTeX command such as \\frac or \\sqrt outside $...$.
3. Short sentences, simple words; explain hard terms in brackets.
4. Focus on the "why" so the student truly learns the concept.
5. Friendly and encouraging."""
    explanation = await chat(
        [{"role": "system", "content": "You are a specialized academic tutor who writes clean Markdown and LaTeX."},
         {"role": "user", "content": prompt}],
        tier="default", temperature=0.5,
    )
    return {
        "success": True,
        "explanation": explanation,
        "questionId": q.get("_id"),
        "suggestions": [
            "I don't understand this part, can you explain further?",
            "Can you give me another example similar to this?",
            "What are the key concepts I should know for this topic?",
        ],
    }


@router.post("/follow-up")
async def follow_up(body: FollowUpRequest, current: User = Depends(get_current_user)) -> dict:
    if not body.message:
        raise HTTPException(status_code=400, detail="Message is required")
    user_class = (body.onboardingData or {}).get("userClass") or "WAEC/JAMB"

    messages: list[dict[str, Any]] = [
        {"role": "system", "content": f"You are a warm, patient Nigerian tutor helping a {user_class} student. Use clean Markdown and LaTeX ($...$)."}
    ]
    for m in (body.history or [])[-10:]:
        if m.get("role") in {"user", "assistant"} and m.get("content"):
            messages.append({"role": m["role"], "content": m["content"]})
    q = body.questionData or {}
    ctx = f'ORIGINAL QUESTION: "{q.get("question_text", "")}"\nCORRECT ANSWER: "{q.get("correct_answer", "")}"\n\n' if q else ""
    messages.append({"role": "user", "content": f"{ctx}STUDENT'S FOLLOW-UP: \"{body.message}\"\n\nAnswer their specific concern clearly and simply. LaTeX for all maths."})

    response = await chat(messages, tier="default", temperature=0.5)
    return {
        "success": True,
        "response": response,
        "suggestions": ["Explain it in simpler terms.", "Show me the steps again.", "I'm ready for the next question."],
    }


@router.post("/generate-answer")
async def generate_answer(body: GenerateAnswerRequest, current: User = Depends(get_current_user)) -> dict:
    q = body.questionData
    if not q:
        raise HTTPException(status_code=400, detail="Question data is required")
    prompt = f"""You are an expert Nigerian tutor. Write a comprehensive, well-structured model answer to this
{q.get('exam_name', '')} {q.get('subject', '')} theory question.

QUESTION: "{q.get('question_text', '')}"

FORMAT: Markdown headers (###), **bold** key terms, LaTeX for ALL maths ($...$ inline, $$...$$ block),
clear step-by-step working, simple language a student can follow."""
    answer = await chat(
        [{"role": "system", "content": "You are a specialized academic tutor who writes clean Markdown and LaTeX."},
         {"role": "user", "content": prompt}],
        tier="default", temperature=0.4,
    )
    return {"success": True, "answer": answer}
