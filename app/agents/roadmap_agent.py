"""Roadmap generation agent (LangGraph).

    gather_context -> plan -> finalize -> END

- gather_context: fetch the exam calendar (horizon) for the student's exams.
- plan:           LLM builds the full plan JSON (phases + weekly levels + tasks),
                  weighted by the diagnostic baseline (weaker subjects get more room).
- finalize:       assign order, week dates, node statuses (first level unlocked),
                  and stable task ids — done in Python, not by the LLM.

Returns a plain dict the router turns into a Roadmap document.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, TypedDict

from langgraph.graph import END, START, StateGraph

from app.core.llm import chat_json
from app.core.subjects import display_name
from app.services.exam_calendar import get_exam_calendar

logger = logging.getLogger("studymaster.roadmap_agent")

# Cap how many weeks we plan in detail in one pass (keeps the LLM output sane;
# the roadmap is regenerated/extended as the student progresses).
MAX_PLANNED_WEEKS = 18


class RoadmapState(TypedDict, total=False):
    exams: list[str]
    subjects: list[str]
    career: str
    target_scores: dict[str, Any]
    baseline: dict[str, Any]
    now: str
    exam_calendar: dict[str, Any]
    exam_date: str
    horizon_weeks: int
    summary: str
    phases: list[dict[str, Any]]
    levels: list[dict[str, Any]]


async def _node_gather_context(state: RoadmapState) -> RoadmapState:
    cal = await get_exam_calendar(state.get("exams", []))
    return {
        "exam_calendar": cal["examCalendar"],
        "exam_date": cal["examDate"],
        "horizon_weeks": cal["horizonWeeks"],
    }


async def _node_plan(state: RoadmapState) -> RoadmapState:
    subjects = state.get("subjects", [])
    subject_lines = "\n".join(f"- {s} ({display_name(s)})" for s in subjects)
    weeks = min(state.get("horizon_weeks", 12), MAX_PLANNED_WEEKS)
    baseline = state.get("baseline") or {}
    breakdown = baseline.get("subjectBreakdown", {}) if isinstance(baseline, dict) else {}
    baseline_txt = (
        ", ".join(f"{k}: {v.get('correct', 0)}/{v.get('total', 0)}" for k, v in breakdown.items())
        or "no baseline data"
    )

    prompt = f"""You are an expert Nigerian exam coach building a personalised study roadmap.

Student is preparing for: {", ".join(state.get("exams", []))}
Target: {state.get("target_scores", {})}
Career goal: {state.get("career", "unspecified")}
Weeks available until the exam: {weeks}
Diagnostic baseline (starting point, NOT a judgement): {baseline_txt}

Subjects to cover (use these EXACT slugs for the "subject" field):
{subject_lines}

Design a week-by-week plan. Weaker baseline subjects get more practice time; every subject
is covered and revisited (spaced repetition). Early weeks build foundations; later weeks are
heavy practice; the final 2-3 weeks are mock exams and revision.

Return STRICT JSON:
{{
  "summary": "2-3 sentences describing the journey and how it reaches the goal.",
  "phases": [
    {{"key": "foundations", "title": "Foundations", "order": 1, "goal": "...", "weeks": [1,2,3,4]}}
  ],
  "levels": [
    {{
      "weekNumber": 1,
      "phaseKey": "foundations",
      "title": "Week 1 · Number & Numeration",
      "goal": "One sentence on the week's aim.",
      "subjectsFocus": ["mathematics", "english-language"],
      "tasks": [
        {{"type": "study", "subject": "mathematics", "topic": "Number bases",
          "title": "Study: Number bases", "description": "...", "targetCount": 0, "estMinutes": 40}},
        {{"type": "practice", "subject": "mathematics", "topic": "Number bases",
          "title": "Practice 20 questions", "description": "...", "targetCount": 20, "estMinutes": 30}}
      ]
    }}
  ]
}}

Rules:
- Produce EXACTLY {weeks} levels (weekNumber 1..{weeks}).
- 3-6 tasks per level. task.type is one of: study, practice, exam, review, milestone.
- task.subject MUST be one of the exact slugs listed above (or null for a general/milestone task).
- Include at least one "exam" (mock) task in the last 3 weeks.
- Topics must be real syllabus topics for the subject in the Nigerian WAEC/JAMB curriculum."""

    data = await chat_json([{"role": "user", "content": prompt}], tier="smart", temperature=0.4)
    if not isinstance(data, dict):
        data = {}
    return {
        "summary": data.get("summary", ""),
        "phases": data.get("phases", []),
        "levels": data.get("levels", []),
    }


def _node_finalize(state: RoadmapState) -> RoadmapState:
    now = datetime.now(timezone.utc)
    subjects = state.get("subjects", [])
    levels = sorted(state.get("levels", []), key=lambda x: x.get("weekNumber", 0))

    finalized: list[dict[str, Any]] = []
    for order, lvl in enumerate(levels, start=1):
        week = lvl.get("weekNumber", order)
        start = now + timedelta(weeks=order - 1)
        end = start + timedelta(days=6)
        tasks = []
        for t_i, t in enumerate(lvl.get("tasks", []), start=1):
            subj = t.get("subject")
            if subj and subj not in subjects:
                subj = None
            tasks.append(
                {
                    "id": f"w{week}-t{t_i}",
                    "type": t.get("type", "study"),
                    "subject": subj,
                    "topic": t.get("topic"),
                    "title": t.get("title", "Task"),
                    "description": t.get("description", ""),
                    "targetCount": int(t.get("targetCount", 0) or 0),
                    "estMinutes": int(t.get("estMinutes", 30) or 30),
                    "status": "available" if order == 1 else "locked",
                }
            )
        finalized.append(
            {
                "order": order,
                "weekNumber": week,
                "phaseKey": lvl.get("phaseKey", ""),
                "title": lvl.get("title", f"Week {week}"),
                "goal": lvl.get("goal", ""),
                "subjectsFocus": [s for s in lvl.get("subjectsFocus", []) if s in subjects],
                "startDate": start,
                "endDate": end,
                "status": "available" if order == 1 else "locked",
                "tasks": tasks,
            }
        )

    summary = state.get("summary") or (
        f"A {len(finalized)}-week plan to your goal, covering "
        f"{', '.join(display_name(s) for s in subjects)}."
    )
    return {"levels": finalized, "summary": summary}


def _build_graph():
    g = StateGraph(RoadmapState)
    g.add_node("gather_context", _node_gather_context)
    g.add_node("plan", _node_plan)
    g.add_node("finalize", _node_finalize)
    g.add_edge(START, "gather_context")
    g.add_edge("gather_context", "plan")
    g.add_edge("plan", "finalize")
    g.add_edge("finalize", END)
    return g.compile()


_graph = _build_graph()


async def generate_roadmap_plan(
    *,
    exams: list[str],
    subjects: list[str],
    career: str | None,
    target_scores: dict[str, Any],
    baseline: dict[str, Any] | None,
) -> RoadmapState:
    """Run the roadmap agent; returns the finalized plan state."""
    return await _graph.ainvoke(
        {
            "exams": exams,
            "subjects": subjects,
            "career": career or "",
            "target_scores": target_scores or {},
            "baseline": baseline or {},
        }
    )
