"""Readiness endpoints — the live "chance of hitting your target".

    GET /api/v1/readiness           recompute + return current readiness (+ trend)
    GET /api/v1/readiness/history   just the trend series
"""

from fastapi import APIRouter, Depends

from app.core.deps import get_current_user
from app.models.user import User
from app.services.readiness_engine import update_readiness

router = APIRouter(prefix="/api/v1/readiness", tags=["readiness"])


@router.get("")
async def get_readiness(current: User = Depends(get_current_user)) -> dict:
    data = await update_readiness(current)
    return {"success": True, "data": data}


@router.get("/history")
async def get_history(current: User = Depends(get_current_user)) -> dict:
    r = current.readiness or {}
    return {"success": True, "data": {"history": r.get("history", [])}}
