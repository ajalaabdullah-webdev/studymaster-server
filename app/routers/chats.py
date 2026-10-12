"""Coach endpoints — ported from Node, upgraded with image (multimodal) support.

    POST /api/v1/chats                 create chat
    GET  /api/v1/chats                 list chats
    GET  /api/v1/chats/{id}            one chat
    POST /api/v1/chats/{id}/messages   send message (optionally with images)
"""

from datetime import datetime, timezone
from typing import Any, Optional

from beanie import PydanticObjectId
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.core.deps import get_current_user
from app.core.llm import chat as llm_chat
from app.models.activity import Activity
from app.models.chat import Chat, ChatMessage
from app.models.question import Question
from app.models.user import User

router = APIRouter(prefix="/api/v1/chats", tags=["coach"])


class CreateChatRequest(BaseModel):
    title: Optional[str] = None
    questionId: Optional[str] = None


class SendMessageRequest(BaseModel):
    message: str
    images: Optional[list[str]] = None  # data URLs (multimodal)


def _chat_dict(c: Chat) -> dict[str, Any]:
    d = c.model_dump(mode="json")
    d["_id"] = str(c.id)
    return d


@router.post("")
async def create_chat(body: CreateChatRequest, current: User = Depends(get_current_user)) -> dict:
    chat = Chat(userId=current.id, title=body.title or "New Chat")
    if body.questionId and PydanticObjectId.is_valid(body.questionId):
        chat.questionId = PydanticObjectId(body.questionId)
    await chat.insert()
    return {"success": True, "data": _chat_dict(chat)}


@router.get("")
async def list_chats(current: User = Depends(get_current_user)) -> dict:
    chats = await Chat.find(Chat.userId == current.id).sort(-Chat.updatedAt).to_list()
    return {"success": True, "count": len(chats), "data": [_chat_dict(c) for c in chats]}


@router.get("/{chat_id}")
async def get_chat(chat_id: str, current: User = Depends(get_current_user)) -> dict:
    return {"success": True, "data": _chat_dict(await _owned(chat_id, current))}


@router.delete("/{chat_id}")
async def delete_chat(chat_id: str, current: User = Depends(get_current_user)) -> dict:
    chat = await _owned(chat_id, current)
    await chat.delete()
    return {"success": True, "message": "Chat deleted"}


@router.post("/{chat_id}/messages")
async def send_message(chat_id: str, body: SendMessageRequest, current: User = Depends(get_current_user)) -> dict:
    if not body.message and not body.images:
        raise HTTPException(status_code=400, detail="Message is required")
    chat = await _owned(chat_id, current)

    chat.messages.append(ChatMessage(role="user", content=body.message))

    # keep an active ai_chat activity running (background signal)
    active = await Activity.find(
        Activity.userId == current.id, Activity.type == "ai_chat", Activity.status == "active"
    ).first_or_none()
    if not active:
        await Activity(userId=current.id, type="ai_chat", status="active",
                       metadata={"chatId": chat_id}).insert()

    user_class = current.onboarding.class_ or "WAEC/JAMB"
    system = (
        f"You are StudyMaster's Coach — a warm, encouraging Nigerian tutor helping a {user_class} student. "
        "Write beautiful, clean markdown: clear headings, bullet points, bold key terms, the occasional emoji. "
        "Explain simply, use local examples, and use LaTeX ($...$) for any maths."
    )
    if chat.questionId:
        q = await Question.get(chat.questionId)
        if q:
            system += f'\nOriginal question context:\n"{q.question_text}"\nCorrect answer: "{q.correct_answer}"'

    messages: list[dict[str, Any]] = [{"role": "system", "content": system}]
    for m in chat.messages[-10:]:
        messages.append({"role": m.role, "content": m.content})

    # Multimodal: attach uploaded images to the latest user turn.
    if body.images:
        parts: list[dict[str, Any]] = [{"type": "text", "text": body.message or "Please look at this image."}]
        for url in body.images[:4]:
            parts.append({"type": "image_url", "image_url": {"url": url}})
        messages[-1] = {"role": "user", "content": parts}

    response = await llm_chat(messages, tier="default", temperature=0.6)

    chat.messages.append(ChatMessage(role="assistant", content=response))
    if chat.title == "New Chat" and len(chat.messages) <= 3 and body.message:
        chat.title = body.message[:30] + ("…" if len(body.message) > 30 else "")
    chat.updatedAt = datetime.now(timezone.utc)
    await chat.save()

    return {
        "success": True,
        "data": _chat_dict(chat),
        "assistantResponse": response,
        "suggestions": ["Can you explain that more simply?", "Give me an example.", "I understand, let's move on."],
    }


async def _owned(chat_id: str, user: User) -> Chat:
    if not PydanticObjectId.is_valid(chat_id):
        raise HTTPException(status_code=400, detail="Invalid chat id")
    chat = await Chat.get(PydanticObjectId(chat_id))
    if not chat or chat.userId != user.id:
        raise HTTPException(status_code=404, detail="Chat not found")
    return chat
