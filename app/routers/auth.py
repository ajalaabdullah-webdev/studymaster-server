"""Auth endpoints — ported 1:1 from the legacy Node backend.

Mounted at ``/api/v1/auth``. Response shapes match what the Next.js
``AuthContext`` already expects (``success``/``token``/``user``/``message``).
"""

from datetime import datetime, timedelta, timezone

import requests as http_requests
from fastapi import APIRouter, HTTPException, Path
from google.auth.exceptions import TransportError
from google.auth.transport import requests as google_requests
from google.oauth2 import id_token as google_id_token

from app.config import settings
from app.core.ids import (
    new_otp,
    new_reset_token,
    new_subscription_id,
    new_user_id,
)
from app.core.security import (
    create_access_token,
    hash_password,
    verify_password,
)
from app.models.user import SubscriptionInfo, User
from app.schemas.auth import (
    AuthResponse,
    ForgotPasswordRequest,
    GoogleAuthRequest,
    LoginRequest,
    RegisterRequest,
    ResendOtpRequest,
    ResetPasswordRequest,
    SimpleResponse,
    VerifyOtpRequest,
)
from app.services.email import send_otp_email, send_reset_email

router = APIRouter(prefix="/api/v1/auth", tags=["auth"])

_GOOGLE_TOKENINFO_URL = "https://oauth2.googleapis.com/tokeninfo"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _trial_subscription(days: int = 1) -> SubscriptionInfo:
    return SubscriptionInfo(
        plan=f"{days}-day-free-trial",
        expirationDate=_now() + timedelta(days=days),
        metadata={},
    )


def _verify_google_token(token: str) -> dict:
    """Verify a Google ID token, with a development-only network fallback.

    The normal google-auth verifier is always preferred. Some local networks
    block its certificate host (www.googleapis.com) while allowing Google's
    tokeninfo endpoint. Google documents tokeninfo for development/debugging,
    so use it only in development and still enforce this app's audience.
    """
    try:
        return google_id_token.verify_oauth2_token(
            token, google_requests.Request(), settings.google_client_id
        )
    except TransportError:
        if settings.is_production:
            raise

        response = http_requests.post(
            _GOOGLE_TOKENINFO_URL,
            data={"id_token": token},
            timeout=10,
        )
        if response.status_code != 200:
            raise ValueError("Invalid Google token")

        info = response.json()
        if info.get("aud") != settings.google_client_id:
            raise ValueError("Google token has the wrong audience")
        return info


@router.post("/register")
async def register(body: RegisterRequest) -> dict:
    existing = await User.find_one(User.email == body.email.lower())
    if existing:
        raise HTTPException(status_code=400, detail="User already exists")

    otp = new_otp()
    user = User(
        username=body.username,
        email=body.email.lower(),
        password=hash_password(body.password),
        userId=new_user_id(),
        subscriptionId=new_subscription_id(),
        isEmailVerified=False,
        otp=otp,
        otpExpires=_now() + timedelta(minutes=10),
        subscription=_trial_subscription(body.trialDays),
    )
    await user.insert()
    send_otp_email(user.email, user.username, otp)

    # Frontend reads `data.data.email` after signup, then routes to verify-otp.
    return {
        "success": True,
        "message": "Registered. Verification code sent to your email.",
        "data": {"email": user.email},
    }


@router.post("/login")
async def login(body: LoginRequest) -> AuthResponse:
    user = await User.find_one(User.email == body.email.lower())
    if not user or not user.password or not verify_password(body.password, user.password):
        raise HTTPException(status_code=401, detail="Invalid credentials")

    token = create_access_token(str(user.id))
    return AuthResponse(success=True, token=token, user=user.public_dict())


@router.post("/google")
async def google_auth(body: GoogleAuthRequest) -> AuthResponse:
    if not settings.google_client_id:
        raise HTTPException(status_code=500, detail="Google login not configured")
    try:
        info = _verify_google_token(body.idToken)
    except (ValueError, http_requests.RequestException):
        raise HTTPException(status_code=401, detail="Invalid Google token")

    email = (info.get("email") or "").lower()
    if not email:
        raise HTTPException(status_code=401, detail="Google token missing email")

    user = await User.find_one(User.email == email)
    if not user:
        user = User(
            username=info.get("name") or email.split("@")[0],
            email=email,
            googleId=info.get("sub"),
            isEmailVerified=True,
            avatar=info.get("picture", ""),
            userId=new_user_id(),
            subscriptionId=new_subscription_id(),
            subscription=_trial_subscription(1),
        )
        await user.insert()
    elif not user.googleId:
        user.googleId = info.get("sub")
        user.isEmailVerified = True
        await user.save()

    token = create_access_token(str(user.id))
    return AuthResponse(
        success=True, token=token, user=user.public_dict(), message="Logged in with Google"
    )


@router.post("/verify-otp")
async def verify_otp(body: VerifyOtpRequest) -> AuthResponse:
    user = await User.find_one(User.email == body.email.lower())
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    if user.isEmailVerified:
        token = create_access_token(str(user.id))
        return AuthResponse(
            success=True, token=token, user=user.public_dict(), message="Already verified"
        )
    if not user.otp or user.otp != body.otp:
        raise HTTPException(status_code=400, detail="Invalid or expired OTP")
    if user.otpExpires and _ensure_aware(user.otpExpires) < _now():
        raise HTTPException(status_code=400, detail="Invalid or expired OTP")

    user.isEmailVerified = True
    user.otp = None
    user.otpExpires = None
    await user.save()

    token = create_access_token(str(user.id))
    return AuthResponse(
        success=True, token=token, user=user.public_dict(), message="Email verified successfully"
    )


@router.post("/resend-otp")
async def resend_otp(body: ResendOtpRequest) -> SimpleResponse:
    user = await User.find_one(User.email == body.email.lower())
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    otp = new_otp()
    user.otp = otp
    user.otpExpires = _now() + timedelta(minutes=10)
    await user.save()
    send_otp_email(user.email, user.username, otp)
    return SimpleResponse(success=True, message="OTP resent successfully")


@router.post("/forgot-password")
async def forgot_password(body: ForgotPasswordRequest) -> SimpleResponse:
    user = await User.find_one(User.email == body.email.lower())
    # Do not reveal whether the account exists.
    if user:
        token = new_reset_token()
        user.resetPasswordToken = token
        user.resetPasswordExpire = _now() + timedelta(hours=1)
        await user.save()
        reset_url = f"{settings.frontend_url}/auth/reset-password/{token}"
        send_reset_email(user.email, user.username, reset_url)
    return SimpleResponse(
        success=True, message="If that email exists, a reset link has been sent."
    )


@router.put("/reset-password/{resettoken}")
async def reset_password(
    body: ResetPasswordRequest, resettoken: str = Path(...)
) -> AuthResponse:
    user = await User.find_one(User.resetPasswordToken == resettoken)
    if not user or not user.resetPasswordExpire or _ensure_aware(user.resetPasswordExpire) < _now():
        raise HTTPException(status_code=400, detail="Invalid or expired token")

    user.password = hash_password(body.password)
    user.resetPasswordToken = None
    user.resetPasswordExpire = None
    await user.save()

    token = create_access_token(str(user.id))
    return AuthResponse(
        success=True, token=token, user=user.public_dict(), message="Password reset successfully"
    )


def _ensure_aware(dt: datetime) -> datetime:
    """Mongo may return naive datetimes; treat them as UTC for comparison."""
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
