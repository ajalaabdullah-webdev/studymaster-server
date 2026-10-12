"""StudyMaster 2.0 API — FastAPI application entrypoint.

This is the new unified Python backend. It talks to the same MongoDB as the
legacy Node service and accepts the same JWTs, so the existing Next.js frontend
can be pointed at it without changes.

Run (dev):  uv run uvicorn app.main:app --reload --port 8000
"""

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.config import settings
from app.db import close_db, init_db

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("studymaster")


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    logger.info("StudyMaster API started (env=%s)", settings.env)
    yield
    await close_db()


app = FastAPI(
    title="StudyMaster 2.0 API",
    version="2.0.0",
    description="Agentic AI study platform backend (FastAPI + LangGraph).",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origin_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# --------------------------------------------------------------------------- #
# Error envelope — mirror the legacy `{ success: false, message }` JSON shape so
# the frontend's existing error handling keeps working.
# --------------------------------------------------------------------------- #
@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException):
    return JSONResponse(
        status_code=exc.status_code,
        content={"success": False, "message": exc.detail},
    )


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    first = exc.errors()[0] if exc.errors() else {}
    msg = first.get("msg", "Invalid request")
    return JSONResponse(
        status_code=422,
        content={"success": False, "message": msg, "errors": exc.errors()},
    )


# --------------------------------------------------------------------------- #
# Health
# --------------------------------------------------------------------------- #
@app.get("/", tags=["health"])
async def root():
    return {"success": True, "message": "StudyMaster 2.0 API is running"}


@app.get("/health", tags=["health"])
async def health():
    return {"success": True, "status": "ok", "env": settings.env}


# --------------------------------------------------------------------------- #
# Routers
# --------------------------------------------------------------------------- #
from app.routers.activity import router as activity_router  # noqa: E402
from app.routers.admin import router as admin_router  # noqa: E402
from app.routers.auth import router as auth_router  # noqa: E402
from app.routers.chats import router as chats_router  # noqa: E402
from app.routers.community import community as community_router  # noqa: E402
from app.routers.community import follow as follow_router  # noqa: E402
from app.routers.community import game as game_router  # noqa: E402
from app.routers.community import profile as profile_router  # noqa: E402
from app.routers.dashboard import router as dashboard_router  # noqa: E402
from app.routers.exams import router as exams_router  # noqa: E402
from app.routers.live import router as live_router  # noqa: E402
from app.routers.live_learning import router as live_learning_router  # noqa: E402
from app.routers.notes import router as notes_router  # noqa: E402
from app.routers.questions import router as questions_router  # noqa: E402
from app.routers.quizzes import router as quizzes_router  # noqa: E402
from app.routers.onboarding import router as onboarding_router  # noqa: E402
from app.routers.readiness import router as readiness_router  # noqa: E402
from app.routers.roadmap import router as roadmap_router  # noqa: E402
from app.routers.speech import router as speech_router  # noqa: E402
from app.routers.user import settings_router, user_router  # noqa: E402

app.include_router(auth_router)
app.include_router(user_router)
app.include_router(settings_router)
app.include_router(onboarding_router)
app.include_router(roadmap_router)
app.include_router(readiness_router)
app.include_router(notes_router)
app.include_router(speech_router)
app.include_router(activity_router)
app.include_router(dashboard_router)
app.include_router(questions_router)
app.include_router(exams_router)
app.include_router(chats_router)
app.include_router(quizzes_router)
app.include_router(community_router)
app.include_router(profile_router)
app.include_router(follow_router)
app.include_router(game_router)
app.include_router(live_router)
app.include_router(live_learning_router)
app.include_router(admin_router)
