"""Request/response schemas for the V2 onboarding flow.

Flow:
  1. POST /onboarding/exams            -> student picks WAEC and/or JAMB
  2. POST /onboarding/research-career  -> types a career; we web-search the
                                          Nigerian subject requirements
  3. POST /onboarding/subjects         -> student confirms the subjects to study
  4. POST /onboarding/diagnostic/start -> we build a ~10-question baseline test
  5. POST /onboarding/diagnostic/submit-> we mark it, store baseline, finish
"""

from typing import Any, Literal, Optional

from pydantic import BaseModel, Field

Exam = Literal["WAEC", "JAMB"]


class ExamsRequest(BaseModel):
    exams: list[Exam] = Field(min_length=1)


class ProfileRequest(BaseModel):
    """Contact details collected during onboarding (phone + school)."""

    phoneNumber: str = Field(min_length=7, max_length=20)
    schoolName: str = Field(min_length=2, max_length=120)


class CareerResearchRequest(BaseModel):
    career: str = Field(min_length=2)
    exams: list[Exam] = Field(min_length=1)


class SubjectRequirement(BaseModel):
    subject: str  # canonical DB slug, e.g. "english-language"
    displayName: str = ""  # human label, e.g. "English Language"
    # "compulsory" (required for the course) or "elective"/"recommended".
    kind: Literal["compulsory", "recommended", "elective"] = "compulsory"
    # Does our question bank actually have questions for this subject+exam?
    available: bool = True
    note: Optional[str] = None


class CareerResearchResponse(BaseModel):
    success: bool = True
    career: str
    # Human-readable summary of what the course needs (from live search + LLM).
    summary: str
    requirements: list[SubjectRequirement]
    # Suggested target, e.g. {"JAMB": 250, "WAEC": "Credits in 5 subjects"}.
    targetScores: dict[str, Any] = Field(default_factory=dict)
    # Source citations for transparency (grounding).
    sources: list[dict[str, str]] = Field(default_factory=list)


class SubjectsRequest(BaseModel):
    exams: list[Exam] = Field(min_length=1)
    career: str
    selectedSubjects: list[str] = Field(min_length=1)
    requiredSubjects: list[str] = Field(default_factory=list)
    targetScores: dict[str, Any] = Field(default_factory=dict)
    researchContext: Optional[dict[str, Any]] = None


class DiagnosticStartRequest(BaseModel):
    subjects: list[str] = Field(default_factory=list)
    exams: list[Exam] = Field(default_factory=list)
    count: int = 10


class DiagnosticAnswer(BaseModel):
    index: int  # position in the session's items list
    answer: str  # option key ("A"/"B"/...) or free text for theory


class DiagnosticSubmitRequest(BaseModel):
    sessionId: str
    answers: list[DiagnosticAnswer]
