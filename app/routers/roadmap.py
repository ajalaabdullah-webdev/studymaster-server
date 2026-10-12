"""Roadmap endpoints — the flexible visual study plan.

    GET  /api/v1/roadmap               fetch active roadmap (auto-generates if none)
    POST /api/v1/roadmap/generate      force (re)generate from current onboarding + baseline
    GET  /api/v1/roadmap/current       the current week's level + progress
    POST /api/v1/roadmap/task/{id}/complete   mark a task done, unlock next level
"""

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException

from app.agents.roadmap_agent import generate_roadmap_plan
from app.core.deps import get_current_user
from app.models.activity import Activity
from app.models.roadmap import Roadmap, RoadmapLevel, RoadmapPhase
from app.models.user import User

router = APIRouter(prefix="/api/v1/roadmap", tags=["roadmap"])


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _serialize(rm: Roadmap) -> dict:
    data = rm.model_dump(mode="json")
    data["id"] = str(rm.id)
    data.pop("userId", None)
    return data


async def _build_and_save(user: User) -> Roadmap:
    ob = user.onboarding
    subjects = ob.selectedSubjects or ob.requiredSubjects
    if not subjects:
        raise HTTPException(
            status_code=400,
            detail="Finish onboarding (pick your subjects) before generating a roadmap.",
        )
    baseline = (user.readiness or {}).get("baseline", {})

    plan = await generate_roadmap_plan(
        exams=ob.exams or ["WAEC", "JAMB"],
        subjects=subjects,
        career=ob.career,
        target_scores=ob.targetScores or {},
        baseline=baseline,
    )

    # Archive any existing active roadmap (keep history).
    existing = await Roadmap.find(Roadmap.userId == user.id, Roadmap.status == "active").to_list()
    prev_version = 0
    for r in existing:
        prev_version = max(prev_version, r.version)
        r.status = "archived"
        await r.save()

    exam_date = plan.get("exam_date")
    roadmap = Roadmap(
        userId=user.id,
        exams=ob.exams or ["WAEC", "JAMB"],
        subjects=subjects,
        career=ob.career,
        targetScores=ob.targetScores or {},
        examDate=datetime.fromisoformat(exam_date) if exam_date else None,
        horizonWeeks=plan.get("horizon_weeks", 0),
        examCalendar=plan.get("exam_calendar", {}),
        phases=[RoadmapPhase(**p) for p in plan.get("phases", [])],
        levels=[RoadmapLevel(**lvl) for lvl in plan.get("levels", [])],
        summary=plan.get("summary", ""),
        version=prev_version + 1,
    )
    await roadmap.insert()
    return roadmap


async def _get_active(user: User) -> Roadmap | None:
    return await Roadmap.find_one(Roadmap.userId == user.id, Roadmap.status == "active")


@router.get("")
async def get_roadmap(current: User = Depends(get_current_user)) -> dict:
    rm = await _get_active(current)
    if rm is None:
        rm = await _build_and_save(current)
    return {"success": True, "data": _serialize(rm)}


@router.post("/generate")
async def generate(current: User = Depends(get_current_user)) -> dict:
    rm = await _build_and_save(current)
    return {"success": True, "message": "Roadmap generated", "data": _serialize(rm)}


@router.get("/current")
async def current_week(current: User = Depends(get_current_user)) -> dict:
    rm = await _get_active(current)
    if rm is None:
        raise HTTPException(status_code=404, detail="No roadmap yet")
    # Current level = first not fully-done level (fallback: last).
    level = next((lvl for lvl in rm.levels if lvl.status != "done"), rm.levels[-1] if rm.levels else None)
    total_tasks = sum(len(lvl.tasks) for lvl in rm.levels)
    done_tasks = sum(1 for lvl in rm.levels for t in lvl.tasks if t.status == "done")
    return {
        "success": True,
        "data": {
            "level": level.model_dump(mode="json") if level else None,
            "progress": {"doneTasks": done_tasks, "totalTasks": total_tasks},
            "examDate": rm.examDate.isoformat() if rm.examDate else None,
        },
    }


@router.post("/task/{task_id}/complete")
async def complete_task(task_id: str, current: User = Depends(get_current_user)) -> dict:
    rm = await _get_active(current)
    if rm is None:
        raise HTTPException(status_code=404, detail="No roadmap yet")

    found = None
    for lvl in rm.levels:
        for t in lvl.tasks:
            if t.id == task_id:
                found = (lvl, t)
                break
        if found:
            break
    if not found:
        raise HTTPException(status_code=404, detail="Task not found")

    level, task = found
    task.status = "done"
    task.completedAt = _now()

    # If all tasks in this level are done, mark level done and unlock the next.
    if all(t.status == "done" for t in level.tasks):
        level.status = "done"
        nxt = next((l for l in rm.levels if l.order == level.order + 1), None)
        if nxt and nxt.status == "locked":
            nxt.status = "available"
            for t in nxt.tasks:
                if t.status == "locked":
                    t.status = "available"
    elif level.status == "available":
        level.status = "in_progress"

    rm.adjustedAt = _now()
    await rm.save()

    await Activity(
        userId=current.id,
        type="roadmap_task",
        status="completed",
        endTime=_now(),
        metadata={"taskId": task_id, "type": task.type, "subject": task.subject},
    ).insert()

    return {"success": True, "data": {"taskId": task_id, "levelStatus": level.status}}
