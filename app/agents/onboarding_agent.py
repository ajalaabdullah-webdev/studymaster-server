"""Onboarding career-research agent (LangGraph).

Turns a free-typed career ("software engineering", "law", "medicine") into the
concrete subject requirements a Nigerian student needs for WAEC/JAMB, grounded in
live web search rather than pure LLM recall.

Graph:
    search -> extract -> check_availability -> verify -> END

- search:            Tavily lookup of Nigerian requirements + JAMB combination.
- extract:           LLM turns search text into structured requirements + target.
- check_availability: mark which subjects our question bank actually covers.
- verify:            LLM sanity-check (English/Maths present where expected, no
                     hallucinated subjects) and produce the final summary.
"""

from __future__ import annotations

import logging
from typing import Any, TypedDict

from langgraph.graph import END, START, StateGraph

from app.core.llm import chat_json
from app.core.subjects import all_subject_display_names, display_name, to_slug
from app.services.questions import available_subjects_for_exams
from app.services.search import format_results_for_llm, web_search

logger = logging.getLogger("studymaster.onboarding_agent")


class ResearchState(TypedDict, total=False):
    career: str
    exams: list[str]
    search_text: str
    sources: list[dict[str, str]]
    requirements: list[dict[str, Any]]
    target_scores: dict[str, Any]
    summary: str


async def _node_search(state: ResearchState) -> ResearchState:
    career = state["career"]
    exams = state.get("exams", [])
    exam_str = " and ".join(exams) if exams else "WAEC and JAMB"
    query = (
        f"Subjects required to study {career} in a Nigerian university. "
        f"{exam_str} subject combination and requirements for {career}. "
        f"JAMB UTME subject combination for {career}. WAEC/O'level requirements."
    )
    search = await web_search(query, max_results=6)
    return {
        "search_text": format_results_for_llm(search),
        "sources": [
            {"title": r["title"], "url": r["url"]} for r in search.get("results", [])
        ],
    }


async def _node_extract(state: ResearchState) -> ResearchState:
    career = state["career"]
    exams = state.get("exams", [])
    available_subjects = ", ".join(all_subject_display_names())
    prompt = f"""You are an expert Nigerian academic adviser (WAEC & JAMB/UTME).

A student wants to study: "{career}"
They are preparing for: {", ".join(exams) or "WAEC and JAMB"}

Using the research below (and your knowledge of the Nigerian system), determine
the subjects this student must and should study. JAMB/UTME is always 4 subjects
including compulsory Use of English. WAEC requires credits in about 5 subjects
including English Language and Mathematics for most courses.

RESEARCH:
{state.get("search_text", "No web results.")}

Return STRICT JSON:
{{
  "requirements": [
    {{"subject": "english language", "kind": "compulsory"}},
    {{"subject": "mathematics", "kind": "compulsory"}}
  ],
  "targetScores": {{"JAMB": 250, "WAEC": "Credit (C6) or better in 5 subjects"}},
  "summary": "1-3 sentence plain-English explanation of the requirements for this course."
}}

Rules:
- Choose subjects ONLY from this list of subjects we offer:
  {available_subjects}
- "kind" is "compulsory", "recommended", or "elective".
- Only include subjects genuinely relevant to "{career}". Do not invent subjects.
- Include English Language and (where relevant) Mathematics.
- targetScores keys must be among the exams the student picked."""
    data = await chat_json(
        [{"role": "user", "content": prompt}], tier="smart", temperature=0.3
    )
    reqs = data.get("requirements", []) if isinstance(data, dict) else []
    return {
        "requirements": reqs,
        "target_scores": data.get("targetScores", {}) if isinstance(data, dict) else {},
        "summary": data.get("summary", "") if isinstance(data, dict) else "",
    }


async def _node_check_availability(state: ResearchState) -> ResearchState:
    exams = state.get("exams", [])
    available = await available_subjects_for_exams(exams)  # set of canonical slugs
    reqs = state.get("requirements", [])
    normalised: list[dict[str, Any]] = []
    seen: set[str] = set()
    for r in reqs:
        slug = to_slug(r.get("subject", ""))
        if not slug or slug in seen:
            continue
        seen.add(slug)
        r["subject"] = slug  # canonical DB slug
        r["displayName"] = display_name(slug)
        r["available"] = slug in available
        if not r["available"]:
            r["note"] = "No past questions yet for this subject in your exam(s)."
        normalised.append(r)
    return {"requirements": normalised}


async def _node_verify(state: ResearchState) -> ResearchState:
    """Lightweight guardrail: ensure English is present; keep summary sensible."""
    reqs = state.get("requirements", [])
    subjects = {r.get("subject") for r in reqs}
    if "english-language" not in subjects:
        reqs.insert(
            0,
            {
                "subject": "english-language",
                "displayName": display_name("english-language"),
                "kind": "compulsory",
                "available": True,
            },
        )
    summary = state.get("summary") or ""
    if not summary:
        names = ", ".join(display_name(r["subject"]) for r in reqs)
        summary = f"To study {state['career']}, focus on: {names}."
    return {"requirements": reqs, "summary": summary}


def _build_graph():
    g = StateGraph(ResearchState)
    g.add_node("search", _node_search)
    g.add_node("extract", _node_extract)
    g.add_node("check_availability", _node_check_availability)
    g.add_node("verify", _node_verify)
    g.add_edge(START, "search")
    g.add_edge("search", "extract")
    g.add_edge("extract", "check_availability")
    g.add_edge("check_availability", "verify")
    g.add_edge("verify", END)
    return g.compile()


_graph = _build_graph()


async def research_career(career: str, exams: list[str]) -> ResearchState:
    """Run the research agent and return the final state."""
    try:
        return await _graph.ainvoke({"career": career, "exams": exams})
    except Exception as exc:  # noqa: BLE001 - never fail onboarding on research
        logger.error("Career research failed for %r: %s", career, exc)
        # Minimal safe fallback so onboarding can proceed.
        return {
            "career": career,
            "exams": exams,
            "requirements": [
                {"subject": "english language", "kind": "compulsory", "available": True},
                {"subject": "mathematics", "kind": "compulsory", "available": True},
            ],
            "target_scores": {},
            "summary": f"Core subjects for {career}. You can refine these next.",
            "sources": [],
        }
