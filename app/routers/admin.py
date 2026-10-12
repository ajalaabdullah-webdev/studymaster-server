"""Admin panel endpoints — ported from Node plus Phase 7 upgrades.

    POST  /api/v1/admin/login                       password -> admin JWT
    GET   /api/v1/admin/dashboard                   stats + growth + tab usage + users
    GET   /api/v1/admin/users?search=&page=&limit=  paginated user list
    POST  /api/v1/admin/subscription/extend         {userId, months, plan}
    PUT   /api/v1/admin/users/{userId}/subscription {plan?, expirationDate?, metadata?}
    PATCH /api/v1/admin/users/{userId}/contacted    {contacted}
    PATCH /api/v1/admin/users/{userId}/advisor      {advisor}      (success advisors)
    GET   /api/v1/admin/games                       game flags
    PUT   /api/v1/admin/games                       {gamingEnabled?, games:[{key,enabled}]}
"""

import hmac
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import jwt as pyjwt
from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel

from app.config import settings
from app.models.activity import Activity
from app.models.platform import get_platform_settings
from app.models.user import User

router = APIRouter(prefix="/api/v1/admin", tags=["admin"])

ACTIVITY_LABELS = {
    "note_creation": "Note Generation",
    "note_study": "Note Study",
    "quiz_study": "Quizzes",
    "past_question_study": "Past Questions",
    "ai_chat": "Coach",
    "app_session": "General Browsing",
    "diagnostic": "Baseline Tests",
    "live_teach": "Live Teaching",
    "exam": "Exams",
    "roadmap_task": "Roadmap",
}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _sign_admin_token() -> str:
    return pyjwt.encode(
        {"isAdminPanel": True, "iat": int(_now().timestamp()),
         "exp": int((_now() + timedelta(hours=12)).timestamp())},
        settings.jwt_secret, algorithm=settings.jwt_algorithm,
    )


async def require_admin(authorization: Optional[str] = Header(default=None)) -> dict:
    token = None
    if authorization and authorization.lower().startswith("bearer "):
        token = authorization.split(" ", 1)[1].strip()
    if not token:
        raise HTTPException(status_code=401, detail="Admin token is required")
    try:
        payload = pyjwt.decode(token, settings.jwt_secret, algorithms=[settings.jwt_algorithm])
    except pyjwt.PyJWTError:
        raise HTTPException(status_code=401, detail="Invalid or expired admin token")
    if not payload.get("isAdminPanel"):
        raise HTTPException(status_code=401, detail="Invalid admin token")
    return payload


class LoginRequest(BaseModel):
    password: str


@router.post("/login")
async def admin_login(body: LoginRequest) -> dict:
    if not settings.admin_password:
        raise HTTPException(status_code=500, detail="Admin password is not configured on server")
    if not hmac.compare_digest(body.password.encode(), settings.admin_password.encode()):
        raise HTTPException(status_code=401, detail="Invalid admin password")
    return {"success": True, "token": _sign_admin_token(), "message": "Admin login successful"}


def _subscription_active(sub: Any) -> bool:
    exp = getattr(sub, "expirationDate", None)
    if not exp:
        return False
    if exp.tzinfo is None:
        exp = exp.replace(tzinfo=timezone.utc)
    return exp > _now()


def _user_row(u: User) -> dict[str, Any]:
    sub = u.subscription
    return {
        "_id": str(u.id),
        "username": u.username,
        "email": u.email,
        "userId": u.userId,
        "phoneNumber": u.phoneNumber or (u.onboarding.phoneNumber or ""),
        "subscription": {
            "plan": sub.plan,
            "expirationDate": sub.expirationDate.isoformat() if sub.expirationDate else None,
            "active": _subscription_active(sub),
        },
        "createdAt": u.createdAt.isoformat() if u.createdAt else None,
        "lastActivity": u.studyStats.lastActivity.isoformat() if u.studyStats.lastActivity else None,
        "contactedByAdmin": u.contactedByAdmin,
        "successAdvisor": u.successAdvisorId or "",
        "onboardingVersion": u.onboarding.version,
    }


@router.get("/dashboard")
async def admin_dashboard(_: dict = Depends(require_admin)) -> dict:
    users = await User.find({"email": {"$regex": ".+@.+"}}).sort(-User.createdAt).to_list()

    total = len(users)
    active_subs = sum(1 for u in users if _subscription_active(u.subscription))
    trial = sum(1 for u in users if u.subscription.plan == "1-day-free-trial")

    # user growth (per day, running total)
    growth_map: dict[str, int] = {}
    for u in sorted(users, key=lambda x: x.createdAt or _now()):
        if u.createdAt:
            key = u.createdAt.strftime("%d %b %Y")
            growth_map[key] = growth_map.get(key, 0) + 1
    running = 0
    user_growth = []
    for label, n in growth_map.items():
        running += n
        user_growth.append({"label": label, "newUsers": n, "totalUsers": running})

    # tab usage
    rows = await Activity.aggregate([
        {"$match": {"status": "completed"}},
        {"$group": {"_id": "$type", "seconds": {"$sum": {"$ifNull": ["$duration", 0]}},
                    "users": {"$addToSet": "$userId"}}},
        {"$sort": {"seconds": -1}},
    ]).to_list()
    top_tabs = [{"tab": ACTIVITY_LABELS.get(r["_id"], r["_id"]),
                 "minutes": round((r.get("seconds") or 0) / 60),
                 "users": len(r.get("users") or [])} for r in rows]

    return {
        "success": True,
        "data": {
            "summary": {
                "totalUsers": total,
                "activeSubscriptions": active_subs,
                "trialUsers": trial,
                "mostUsedTab": top_tabs[0]["tab"] if top_tabs else "-",
            },
            "userGrowth": user_growth,
            "topTabs": top_tabs,
            "users": [_user_row(u) for u in users],
        },
    }


@router.get("/users")
async def admin_users(search: str = "", page: int = 1, limit: int = 25,
                      _: dict = Depends(require_admin)) -> dict:
    page = max(page, 1)
    limit = min(max(limit, 1), 200)
    query: dict[str, Any] = {"email": {"$regex": ".+@.+"}}
    if search.strip():
        rx = re.escape(search.strip())
        query = {"$and": [query, {"$or": [
            {"username": {"$regex": rx, "$options": "i"}},
            {"email": {"$regex": rx, "$options": "i"}},
            {"userId": {"$regex": rx, "$options": "i"}},
        ]}]}
    total = await User.find(query).count()
    users = await User.find(query).sort(-User.createdAt).skip((page - 1) * limit).limit(limit).to_list()
    pages = (total + limit - 1) // limit
    return {
        "success": True,
        # legacy shape the admin frontend reads:
        "count": total,
        "totalPages": pages,
        "pagination": {"total": total, "page": page, "pages": pages},
        "data": [_user_row(u) for u in users],
    }


class ExtendRequest(BaseModel):
    userId: str
    months: int = 1
    plan: Optional[str] = None


@router.post("/subscription/extend")
async def extend_subscription(body: ExtendRequest, _: dict = Depends(require_admin)) -> dict:
    user = await User.find_one(User.userId == body.userId)
    if not user:
        raise HTTPException(status_code=404, detail=f"User not found with userId {body.userId}")
    months = max(body.months or 1, 1)
    now = _now()
    current = user.subscription.expirationDate
    if current and current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    base = current if (current and current > now) else now
    # add calendar months
    month = base.month - 1 + months
    year = base.year + month // 12
    month = month % 12 + 1
    day = min(base.day, 28)
    new_expiry = base.replace(year=year, month=month, day=day)

    user.subscription.plan = (body.plan or "basic").strip()
    user.subscription.expirationDate = new_expiry
    meta = dict(user.subscription.metadata or {})
    meta.update({"lastExtendedAt": now.isoformat(), "lastExtendedBy": "admin-panel",
                 "extensionMonths": months})
    user.subscription.metadata = meta
    await user.save()
    return {"success": True, "message": f"Subscription extended by {months} month(s)",
            "data": _user_row(user)}


class SubscriptionUpdate(BaseModel):
    plan: Optional[str] = None
    expirationDate: Optional[datetime] = None
    metadata: Optional[dict[str, Any]] = None


@router.put("/users/{user_id}/subscription")
async def update_subscription(user_id: str, body: SubscriptionUpdate,
                              _: dict = Depends(require_admin)) -> dict:
    user = await User.find_one(User.userId == user_id)
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    if body.plan is not None:
        user.subscription.plan = body.plan
    if body.expirationDate is not None:
        user.subscription.expirationDate = body.expirationDate
    if body.metadata is not None:
        user.subscription.metadata = {**(user.subscription.metadata or {}), **body.metadata}
    await user.save()
    return {"success": True, "message": "Subscription updated", "data": _user_row(user)}


class ContactedRequest(BaseModel):
    contacted: bool


@router.patch("/users/{user_id}/contacted")
async def mark_contacted(user_id: str, body: ContactedRequest,
                         _: dict = Depends(require_admin)) -> dict:
    user = await User.find_one(User.userId == user_id)
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    user.contactedByAdmin = body.contacted
    await user.save()
    return {"success": True, "data": _user_row(user)}


class AdvisorRequest(BaseModel):
    advisor: str  # advisor's name/handle; empty string clears


@router.patch("/users/{user_id}/advisor")
async def assign_advisor(user_id: str, body: AdvisorRequest,
                         _: dict = Depends(require_admin)) -> dict:
    """Success advisors: a human assigned to check in on each student."""
    user = await User.find_one(User.userId == user_id)
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    user.successAdvisorId = body.advisor.strip() or None
    await user.save()
    return {"success": True, "data": _user_row(user)}


# ------------------------------ game toggles ------------------------------- #
class GamesUpdate(BaseModel):
    gamingEnabled: Optional[bool] = None
    games: Optional[list[dict[str, Any]]] = None  # [{key, enabled}]


@router.get("/games")
async def get_games(_: dict = Depends(require_admin)) -> dict:
    ps = await get_platform_settings()
    return {"success": True, "data": {
        "gamingEnabled": ps.gamingEnabled,
        "games": [g.model_dump() for g in ps.games],
    }}


@router.put("/games")
async def update_games(body: GamesUpdate, _: dict = Depends(require_admin)) -> dict:
    ps = await get_platform_settings()
    if body.gamingEnabled is not None:
        ps.gamingEnabled = body.gamingEnabled
    if body.games:
        flags = {g.get("key"): bool(g.get("enabled")) for g in body.games if g.get("key")}
        for g in ps.games:
            if g.key in flags:
                g.enabled = flags[g.key]
    ps.updatedAt = _now()
    await ps.save()
    return {"success": True, "data": {
        "gamingEnabled": ps.gamingEnabled,
        "games": [g.model_dump() for g in ps.games],
    }}
