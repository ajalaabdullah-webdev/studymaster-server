"""Activity tracking — ported from the legacy Node backend.

The frontend pings these as the student moves through the app; the resulting
stream feeds the readiness engine (consistency/active days) and the dashboard.

    POST /api/v1/activity/start  {type, metadata?}
    POST /api/v1/activity/stop   {type, metadata?}
"""

from datetime import datetime, timezone
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.core.deps import get_current_user
from app.models.activity import Activity
from app.models.user import User

router = APIRouter(prefix="/api/v1/activity", tags=["activity"])


class ActivityRequest(BaseModel):
    type: str
    metadata: Optional[dict[str, Any]] = None


def _now() -> datetime:
    return datetime.now(timezone.utc)


@router.get("")
async def list_activities(
    page: int = 1,
    limit: int = 30,
    current: User = Depends(get_current_user),
) -> dict:
    """Full activity history for the Activity page (newest first, paginated).

    Shows real STUDY activities only — plumbing events like ``app_session``
    (tab switches) are tracked for the readiness engine but hidden here.
    """
    limit = min(max(limit, 1), 100)
    query = {"userId": current.id, "type": {"$ne": "app_session"}}
    total = await Activity.find(query).count()
    items = await (
        Activity.find(query)
        .sort(-Activity.createdAt)
        .skip((page - 1) * limit)
        .limit(limit)
        .to_list()
    )
    return {
        "success": True,
        "pagination": {"total": total, "page": page, "pages": (total + limit - 1) // limit},
        "data": [{**a.model_dump(mode="json"), "_id": str(a.id)} for a in items],
    }


@router.post("/start")
async def start_activity(body: ActivityRequest, current: User = Depends(get_current_user)) -> dict:
    # Safety: close any dangling active activity of the same type.
    await Activity.find(
        Activity.userId == current.id, Activity.type == body.type, Activity.status == "active"
    ).update_many({"$set": {"status": "completed", "endTime": _now()}})

    activity = Activity(
        userId=current.id, type=body.type, status="active",
        startTime=_now(), metadata=body.metadata or {},
    )
    await activity.insert()
    return {"success": True, "data": {"id": str(activity.id), "type": activity.type}}


@router.post("/stop")
async def stop_activity(body: ActivityRequest, current: User = Depends(get_current_user)) -> dict:
    activity = await Activity.find(
        Activity.userId == current.id, Activity.type == body.type, Activity.status == "active"
    ).sort(-Activity.startTime).first_or_none()
    if not activity:
        raise HTTPException(status_code=404, detail="No active activity found for this type")

    end = _now()
    start = activity.startTime
    if start and start.tzinfo is None:
        start = start.replace(tzinfo=timezone.utc)
    duration = max(0, int((end - start).total_seconds())) if start else 0

    activity.endTime = end
    activity.duration = duration
    activity.status = "completed"
    if body.metadata:
        activity.metadata = {**(activity.metadata or {}), **body.metadata}
    await activity.save()

    # Keep the user's lightweight studyStats in step (dashboard + streaks).
    stats = current.studyStats
    stats.totalStudyTime = int(stats.totalStudyTime or 0) + duration
    stats.lastActivity = end
    counts = stats.activityCounts
    if hasattr(counts, activity.type):
        setattr(counts, activity.type, int(getattr(counts, activity.type) or 0) + 1)
    await current.save()

    return {"success": True, "data": {"id": str(activity.id), "duration": duration}}
