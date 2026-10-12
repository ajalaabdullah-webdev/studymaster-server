"""Beanie document models.

``ALL_DOCUMENT_MODELS`` is the registry passed to ``init_beanie``. Models are
added here as each build phase ports its collection into the Python backend.
"""

from app.models.activity import Activity
from app.models.diagnostic import DiagnosticSession
from app.models.chat import Chat
from app.models.community import Comment, Follower, GameAttempt, Post
from app.models.exam import Exam
from app.models.note import Note
from app.models.live_lesson import LiveLesson
from app.models.platform import PlatformSettings
from app.models.question import Question
from app.models.quiz import Quiz
from app.models.roadmap import Roadmap
from app.services.topic_search import TopicKeywords
from app.models.user import User

ALL_DOCUMENT_MODELS = [
    User,
    Question,
    Activity,
    DiagnosticSession,
    Roadmap,
    Exam,
    Quiz,
    Note,
    LiveLesson,
    Post,
    Comment,
    Follower,
    GameAttempt,
    Chat,
    TopicKeywords,
    PlatformSettings,
]

__all__ = [
    "User",
    "Question",
    "Activity",
    "DiagnosticSession",
    "Roadmap",
    "Exam",
    "Quiz",
    "Note",
    "LiveLesson",
    "ALL_DOCUMENT_MODELS",
]
