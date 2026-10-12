"""User + account endpoints.

- ``GET  /api/v1/user/me``          — refresh current user (AuthContext.refreshUser)
- ``PUT  /api/v1/user/onboarding``  — backward-compatible onboarding update
- ``PUT  /api/v1/settings/change-password``
"""

from typing import Any

from fastapi import APIRouter, Body, Depends, HTTPException

from app.core.deps import get_current_user
from app.core.security import hash_password, verify_password
from app.models.user import User
from app.schemas.auth import ChangePasswordRequest, SimpleResponse

user_router = APIRouter(prefix="/api/v1/user", tags=["user"])
settings_router = APIRouter(prefix="/api/v1/settings", tags=["settings"])


@user_router.get("/me")
async def get_me(current: User = Depends(get_current_user)) -> SimpleResponse:
    return SimpleResponse(success=True, data=current.public_dict())


@user_router.put("/onboarding")
async def update_onboarding(
    payload: dict[str, Any] = Body(...),
    current: User = Depends(get_current_user),
) -> dict:
    """Backward-compatible onboarding update (V1 shape: class/schoolName/phoneNumber/goal).

    The richer V2 onboarding (exam pick, career research, diagnostic) has its own
    endpoints under ``/api/v1/onboarding``; this remains so nothing breaks.
    """
    onboarding = current.onboarding
    # Accept and store any provided legacy/simple fields.
    field_map = {
        "class": "class_",
        "schoolName": "schoolName",
        "phoneNumber": "phoneNumber",
        "goal": "goal",
        "bestSubject": "bestSubject",
        "weakSubject": "weakSubject",
        "age": "age",
    }
    for incoming, attr in field_map.items():
        if incoming in payload and payload[incoming] is not None:
            setattr(onboarding, attr, payload[incoming])

    if payload.get("phoneNumber"):
        current.phoneNumber = payload["phoneNumber"]

    if not onboarding.version:
        onboarding.version = "v1"

    # Mark complete when the essentials are present (matches legacy rule).
    if onboarding.class_ and onboarding.schoolName and onboarding.goal:
        current.onboardingCompleted = True

    current.onboarding = onboarding
    await current.save()

    return {
        "success": True,
        "message": "Onboarding updated successfully",
        "data": {
            "onboarding": current.onboarding.model_dump(by_alias=True),
            "onboardingCompleted": current.onboardingCompleted,
            "phoneNumber": current.phoneNumber,
        },
    }


@settings_router.put("/change-password")
async def change_password(
    body: ChangePasswordRequest,
    current: User = Depends(get_current_user),
) -> SimpleResponse:
    if not current.password or not verify_password(body.currentPassword, current.password):
        raise HTTPException(status_code=400, detail="Current password is incorrect")
    current.password = hash_password(body.newPassword)
    await current.save()
    return SimpleResponse(success=True, message="Password changed successfully")
