"""User document — maps to the existing ``users`` collection.

Field names are kept in camelCase to exactly mirror the legacy Mongoose schema
and the JSON contract the Next.js frontend already consumes (e.g.
``onboardingCompleted``, ``studyStats``). Legacy fields are preserved; V2 fields
are added as optional so existing (expired) user records load cleanly.
"""

from datetime import datetime, timezone
from typing import Any, Literal, Optional

from beanie import Document
from pydantic import BaseModel, Field


# --------------------------------------------------------------------------- #
# Embedded sub-documents
# --------------------------------------------------------------------------- #
class Onboarding(BaseModel):
    """Onboarding profile.

    Legacy V1 fields (class/school/goal) are retained for the 50 existing users.
    V2 fields capture the exam-focused, goal-driven onboarding.
    """

    # --- Legacy V1 ---
    class_: Optional[str] = Field(default=None, alias="class")
    bestSubject: Optional[str] = None
    weakSubject: Optional[str] = None
    schoolName: Optional[str] = None
    age: Optional[int] = None
    goal: Optional[str] = None
    phoneNumber: Optional[str] = None

    # --- V2: exam + career focus ---
    # Exams the student is preparing for (subset of WAEC / JAMB).
    exams: list[Literal["WAEC", "JAMB"]] = Field(default_factory=list)
    # Free-typed career/course, e.g. "software engineering", "law", "medicine".
    career: Optional[str] = None
    # Subjects required for that career (resolved via live web search + LLM).
    requiredSubjects: list[str] = Field(default_factory=list)
    # Subjects the student actually chose to register/study.
    selectedSubjects: list[str] = Field(default_factory=list)
    # Target scores per exam, e.g. {"JAMB": 280, "WAEC": "5 credits incl. Maths & English"}.
    targetScores: dict[str, Any] = Field(default_factory=dict)
    # ISO date strings for upcoming exam sittings, e.g. {"JAMB": "2026-04-15"}.
    examDates: dict[str, Any] = Field(default_factory=dict)
    # Provenance of the requirement research (search snippets/citations) for transparency.
    researchContext: Optional[dict[str, Any]] = None
    # Which onboarding version the student completed.
    version: Optional[str] = None  # "v1" | "v2"

    model_config = {"populate_by_name": True, "extra": "allow"}


class SubscriptionInfo(BaseModel):
    plan: str = "1-day-free-trial"
    expirationDate: Optional[datetime] = None
    metadata: dict[str, Any] = Field(default_factory=dict)

    model_config = {"extra": "allow"}


class ActivityCounts(BaseModel):
    note_creation: int = 0
    note_study: int = 0
    quiz_study: int = 0
    past_question_study: int = 0
    ai_chat: int = 0

    model_config = {"extra": "allow"}


class StudyStats(BaseModel):
    totalStudyTime: int = 0  # seconds
    lastActivity: Optional[datetime] = None
    activityCounts: ActivityCounts = Field(default_factory=ActivityCounts)

    model_config = {"extra": "allow"}


class FcmToken(BaseModel):
    token: str
    platform: str = "web"
    deviceName: str = ""
    lastSeenAt: Optional[datetime] = None

    model_config = {"extra": "allow"}


# --------------------------------------------------------------------------- #
# User document
# --------------------------------------------------------------------------- #
class User(Document):
    # NOTE: this Mongo cluster is shared with another (insurance) app whose docs
    # also live in `users`. Core fields carry safe defaults so a foreign/corrupt
    # record can't crash a bulk read. Real signup input is still validated as a
    # proper email at the API-schema layer (see schemas/auth.py). Real users are
    # always keyed by their real email/id, so this tolerance is invisible to them.
    username: str = ""
    email: str = ""
    phoneNumber: str = ""
    # bcrypt hash; excluded from API responses. Optional for Google-only accounts.
    password: Optional[str] = None
    googleId: Optional[str] = None
    isEmailVerified: bool = False
    otp: Optional[str] = None
    otpExpires: Optional[datetime] = None

    userId: str = ""  # business id, format "U-xxxxxxxx"
    subscriptionId: str = ""  # business id, format "S-xxxxxxxx"
    role: Literal["user", "admin"] = "user"

    onboarding: Onboarding = Field(default_factory=Onboarding)
    onboardingCompleted: bool = False
    # Product tour ("this is your roadmap, this is Live Teach…"). Stored on the
    # user, not localStorage, so it doesn't repeat on a second device — and so a
    # migrated V1 user gets the tour once, after their V2 onboarding.
    hasCompletedTour: bool = False
    contactedByAdmin: bool = False

    subscription: SubscriptionInfo = Field(default_factory=SubscriptionInfo)
    createdAt: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    studyStats: StudyStats = Field(default_factory=StudyStats)

    resetPasswordToken: Optional[str] = None
    resetPasswordExpire: Optional[datetime] = None
    avatar: str = ""
    bio: str = ""
    fcmTokens: list[FcmToken] = Field(default_factory=list)

    # --- V2 additions ---
    # Assigned human success advisor (admin user _id) — the "you're not alone" feature.
    successAdvisorId: Optional[str] = None
    # Latest computed readiness probabilities per exam, e.g. {"JAMB": 0.42}.
    readiness: dict[str, Any] = Field(default_factory=dict)

    class Settings:
        name = "users"
        # Indexes already exist in the DB (created by the legacy Mongoose schema);
        # we don't let Beanie manage them to avoid name/spec conflicts.
        keep_nulls = False

    model_config = {"populate_by_name": True, "extra": "allow"}

    # -- convenience helpers --
    def public_dict(self) -> dict[str, Any]:
        """Serialise for API responses (mirrors the legacy `user` JSON, no secrets)."""
        data = self.model_dump(mode="json", by_alias=True, exclude={"password", "otp"})
        data["_id"] = str(self.id)
        data["id"] = str(self.id)
        data.pop("resetPasswordToken", None)
        data.pop("resetPasswordExpire", None)
        return data
