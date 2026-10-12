"""Request/response schemas for auth + onboarding endpoints.

Response envelopes intentionally mirror the legacy Node backend's JSON shape
(``{success, token, user, message}``) so the existing Next.js frontend works
against this service without changes.
"""

from typing import Any, Optional

from pydantic import BaseModel, EmailStr, Field


# --- requests ---
class RegisterRequest(BaseModel):
    username: str = Field(min_length=1)
    email: EmailStr
    password: str = Field(min_length=6)
    trialDays: int = 1


class LoginRequest(BaseModel):
    email: EmailStr
    password: str


class GoogleAuthRequest(BaseModel):
    idToken: str


class VerifyOtpRequest(BaseModel):
    email: EmailStr
    otp: str


class ResendOtpRequest(BaseModel):
    email: EmailStr


class ForgotPasswordRequest(BaseModel):
    email: EmailStr


class ResetPasswordRequest(BaseModel):
    password: str = Field(min_length=6)


class ChangePasswordRequest(BaseModel):
    currentPassword: str
    newPassword: str = Field(min_length=6)


# --- responses ---
class AuthResponse(BaseModel):
    success: bool = True
    token: Optional[str] = None
    user: Optional[dict[str, Any]] = None
    message: Optional[str] = None
    needsVerification: Optional[bool] = None
    email: Optional[str] = None


class SimpleResponse(BaseModel):
    success: bool = True
    message: Optional[str] = None
    data: Optional[Any] = None
