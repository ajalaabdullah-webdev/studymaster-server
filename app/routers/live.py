"""Live Teaching WebSocket bridge.

    WS /api/v1/notes/{note_id}/live-teach?token=<jwt>

Browser <-> this endpoint <-> Gemini Live.

Client -> server JSON messages:
    {"type": "audio", "data": "<b64 pcm16 @16kHz mono>"}   mic audio while held
    {"type": "text",  "text": "..."}                        optional text input
    {"type": "start"}                                       begin the lesson

Server -> client JSON messages:
    {"type": "ready", "segments": N}
    {"type": "audio", "data": "<b64 pcm16 @24kHz mono>"}
    {"type": "segment", "index": i}          highlight this block
    {"type": "interrupted"}                  student barge-in: stop local playback
    {"type": "turn_complete"}
    {"type": "error", "message": "..."}
"""

import asyncio
import base64
import contextlib
import json
import logging
import time
from datetime import datetime, timezone

import jwt as pyjwt
from beanie import PydanticObjectId
from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from google import genai
from google.genai import types

from app.config import settings
from app.core.security import decode_access_token
from app.models.activity import Activity
from app.models.note import Note
from app.models.user import User
from app.services.live_teach import build_config, build_segments

logger = logging.getLogger("studymaster.live")

# NON_BLOCKING tools still need their (SILENT) responses — withholding them
# makes the model stall mid-lesson and the server kill the session ~50s in.
LIVE_SEND_TOOL_RESPONSES = True

# Latest resumable Gemini handle per (user, note), kept for a grace window so a
# page refresh / client reconnect rejoins the SAME lesson. In-memory is fine:
# one backend process, and Google itself keeps handles alive ~2 hours.
_RESUME_HANDLES: dict[str, tuple[str, float]] = {}
RESUME_TTL_SECONDS = 600.0

RECONNECT_BACKOFFS = [0.0, 0.5, 1.5, 4.0]

CONTINUE_NUDGE = (
    "[system message: the audio link blipped for a moment — the student never left. "
    "Continue teaching exactly where you stopped, mid-sentence if needed. Do NOT "
    "greet the student, do NOT say welcome back, do NOT summarise or restart. "
    "Just carry on naturally.]"
)
NEXT_PART_NUDGE = (
    "[system message: the student has caught up — continue with the next segment now. "
    "Call show_segment for it, teach just that one segment, then stop and wait again. "
    "Do not greet, do not recap, do not ask permission.]"
)
SEG_FALLBACK = (
    "[system message: continue teaching from segment {n} — call show_segment first, "
    "then dive straight back into the material. Do NOT greet the student or say "
    "welcome back; they never left.]"
)

router = APIRouter()


async def _auth_user(token: str | None) -> User | None:
    if not token:
        return None
    try:
        payload = decode_access_token(token)
    except pyjwt.PyJWTError:
        return None
    uid = payload.get("id")
    if not uid or not PydanticObjectId.is_valid(uid):
        return None
    return await User.get(PydanticObjectId(uid))


@router.websocket("/api/v1/notes/{note_id}/live-teach")
async def live_teach(ws: WebSocket, note_id: str):
    await ws.accept()
    token = ws.query_params.get("token")
    user = await _auth_user(token)
    if not user:
        await ws.send_text(json.dumps({"type": "error", "message": "Not authorized"}))
        await ws.close()
        return
    if not PydanticObjectId.is_valid(note_id):
        await ws.send_text(json.dumps({"type": "error", "message": "Invalid note"}))
        await ws.close()
        return
    note = await Note.get(PydanticObjectId(note_id))
    if not note or note.userId != user.id:
        await ws.send_text(json.dumps({"type": "error", "message": "Note not found"}))
        await ws.close()
        return

    segments = build_segments(note)
    if not segments:
        await ws.send_text(json.dumps({"type": "error", "message": "Nothing to teach yet"}))
        await ws.close()
        return

    client = genai.Client(api_key=settings.gemini_api_key)

    started = datetime.now(timezone.utc)
    activity = Activity(userId=user.id, type="live_teach", status="active",
                        startTime=started, metadata={"noteId": str(note.id), "title": note.title})
    await activity.insert()

    # If this user reopened the same note within the grace window (page refresh,
    # network blip, backend redeploy), rejoin the SAME Gemini session so the
    # teacher continues instead of starting over.
    resume_key = f"{user.id}:{note.id}"
    stored = _RESUME_HANDLES.get(resume_key)
    initial_handle = stored[0] if stored and (time.monotonic() - stored[1]) < RESUME_TTL_SECONDS else None

    # Shared mutable state across Gemini reconnects (client WS outlives them all).
    state = {
        "handle": initial_handle,   # newest resumable handle from Gemini
        "mid_turn": False,          # audio flowed since the last turn boundary
        "rotate": False,            # GoAway received -> swap connections at a safe point
        "last_seg": -1,             # last segment we told the browser to highlight
        "ctx_lost": False,          # handle expired -> resumed WITHOUT server context
        "rejoined": initial_handle is not None,  # convert the client's next "start" into a nudge
        "started": False,           # lesson has begun (a start/nudge was sent)
    }

    async def client_to_gemini(session) -> str:
        try:
            while True:
                raw = await ws.receive_text()
                try:
                    msg = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                mtype = msg.get("type")
                if mtype == "audio" and msg.get("data"):
                    await session.send_realtime_input(
                        audio=types.Blob(data=base64.b64decode(msg["data"]),
                                         mime_type="audio/pcm;rate=16000")
                    )
                elif mtype == "mic_start":
                    # Student pressed the mic: formally open their turn — this
                    # also interrupts the teacher mid-speech.
                    await session.send_realtime_input(activity_start=types.ActivityStart())
                elif mtype in {"mic_end", "audio_end"}:
                    # Released: close the turn — the model MUST now respond.
                    await session.send_realtime_input(activity_end=types.ActivityEnd())
                elif mtype == "text" and msg.get("text"):
                    await session.send_client_content(
                        turns={"role": "user", "parts": [{"text": msg["text"]}]},
                        turn_complete=True,
                    )
                elif mtype == "continue":
                    # Pacing cue: the browser has nearly finished playing the last
                    # part, so ask for the next one. This keeps generation roughly
                    # in step with what the student is actually hearing.
                    await session.send_client_content(
                        turns={"role": "user", "parts": [{"text": NEXT_PART_NUDGE}]},
                        turn_complete=True,
                    )
                elif mtype == "start":
                    state["started"] = True
                    if state["rejoined"]:
                        # Same lesson, new browser socket: the model still has the
                        # full context — just tell it to carry on.
                        state["rejoined"] = False
                        await session.send_client_content(
                            turns={"role": "user", "parts": [{"text": CONTINUE_NUDGE}]},
                            turn_complete=True,
                        )
                    else:
                        await session.send_client_content(
                            turns={"role": "user", "parts": [{"text": "I'm ready — please start teaching me from the beginning."}]},
                            turn_complete=True,
                        )
        except (WebSocketDisconnect, RuntimeError):
            return "client_gone"
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — send failed: Gemini leg is dead
            logger.warning("live-teach up leg error: %s", exc)
            return "up_error"

    async def gemini_to_client(session) -> str:
        try:
            while True:
                # session.receive() iterates ONE model turn then ends — the outer
                # while keeps the session alive across turns.
                async for resp in session.receive():
                    if resp.session_resumption_update:
                        upd = resp.session_resumption_update
                        # `resumable` is often unset (None) even on valid handles —
                        # verified working — so key off the handle itself. Handles
                        # only start arriving after the first turn completes.
                        if upd.new_handle and upd.resumable is not False:
                            state["handle"] = upd.new_handle
                            state["ctx_lost"] = False
                    if resp.go_away is not None:
                        # Google resets every connection ~10 min in; it warns us
                        # first. Swap to a fresh connection at the next safe point.
                        left = getattr(resp.go_away, "time_left", None)
                        logger.info("live-teach GoAway (time_left=%s) — rotating", left)
                        state["rotate"] = True
                    if resp.tool_call:
                        responses = []
                        for fc in resp.tool_call.function_calls:
                            if fc.name == "show_segment":
                                try:
                                    idx = int((fc.args or {}).get("index", -1))
                                except (TypeError, ValueError):
                                    idx = -1
                                if 0 <= idx < len(segments):
                                    state["last_seg"] = idx
                                    await ws.send_text(json.dumps({"type": "segment", "index": idx}))
                            responses.append(types.FunctionResponse(
                                id=fc.id, name=fc.name, response={"ok": True},
                                # SILENT: don't interrupt the model's speech —
                                # show_segment is a fire-and-forget highlight.
                                scheduling=types.FunctionResponseScheduling.SILENT))
                        if LIVE_SEND_TOOL_RESPONSES:
                            await session.send_tool_response(function_responses=responses)
                        continue
                    sc = resp.server_content
                    if not sc:
                        continue
                    if sc.interrupted:
                        await ws.send_text(json.dumps({"type": "interrupted"}))
                    if sc.model_turn:
                        for part in sc.model_turn.parts:
                            if part.inline_data and part.inline_data.data:
                                state["mid_turn"] = True
                                await ws.send_text(json.dumps({
                                    "type": "audio",
                                    "data": base64.b64encode(part.inline_data.data).decode(),
                                }))
                    if sc.turn_complete:
                        state["mid_turn"] = False
                        await ws.send_text(json.dumps({
                            "type": "turn_complete",
                            # Once the last segment has been taught, the client
                            # stops sending pacing cues.
                            "final": state["last_seg"] >= len(segments) - 1,
                        }))
                        if state["rotate"]:
                            return "rotate"
                if state["rotate"] and not state["mid_turn"]:
                    return "rotate"
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — 1006/1007 aborts land here
            logger.warning("live-teach gemini leg dropped: %s", exc)
            return "gemini_dead"

    first_connect = True
    connect_failures = 0
    death_times: list[float] = []
    try:
        while True:
            config = build_config(note, segments, resume_handle=state["handle"])
            try:
                async with client.aio.live.connect(
                    model=settings.gemini_live_model, config=config
                ) as session:
                    connect_failures = 0
                    if first_connect:
                        first_connect = False
                        await ws.send_text(json.dumps({"type": "ready", "segments": len(segments)}))
                    elif state["started"]:
                        # Transparent in-place reconnect. With a resumption handle the
                        # model still has full context — just tell it to carry on. If
                        # the old session died before issuing a handle, this is a FRESH
                        # session: point it at the last highlighted segment instead.
                        if state["handle"] and not state["ctx_lost"]:
                            nudge = CONTINUE_NUDGE
                        else:
                            state["ctx_lost"] = False
                            nudge = SEG_FALLBACK.format(n=max(0, state["last_seg"]))
                        await session.send_client_content(
                            turns={"role": "user", "parts": [{"text": nudge}]},
                            turn_complete=True,
                        )
                        state["mid_turn"] = False
                    state["rotate"] = False

                    up = asyncio.create_task(client_to_gemini(session), name="up")
                    down = asyncio.create_task(gemini_to_client(session), name="down")
                    done, pending = await asyncio.wait({up, down}, return_when=asyncio.FIRST_COMPLETED)
                    for t in pending:
                        t.cancel()
                        # CancelledError is a BaseException — suppress(Exception)
                        # would let it escape and kill the whole endpoint.
                        with contextlib.suppress(asyncio.CancelledError, Exception):
                            await t
                    reasons = {t.get_name(): t.result() for t in done}
                logger.info("live-teach cycle ended: %s (handle=%s)", reasons, bool(state["handle"]))

                if reasons.get("up") == "client_gone":
                    break  # student left — the only non-error exit

                # Gemini leg ended (rotate / abort) but the student is still here:
                # reconnect with the resumption handle. Guard against churn.
                if reasons.get("down") == "gemini_dead" or reasons.get("up") == "up_error":
                    now = time.monotonic()
                    death_times[:] = [d for d in death_times if now - d < 60] + [now]
                    if len(death_times) >= 6:
                        logger.error("live-teach: gemini churning (%d drops/min) — giving up", len(death_times))
                        await ws.send_text(json.dumps({"type": "error", "message": "Live session ended unexpectedly."}))
                        break
                    # Escalating pause so a rapid abort/quota storm doesn't spin
                    # (the client's jitter buffer covers a sub-second recovery).
                    await asyncio.sleep(RECONNECT_BACKOFFS[min(len(death_times) - 1, len(RECONNECT_BACKOFFS) - 1)])
            except WebSocketDisconnect:
                break
            except Exception as exc:  # noqa: BLE001 — connect() itself failed
                connect_failures += 1
                logger.warning("live-teach connect failure #%d: %s", connect_failures, exc)
                if state["handle"] and connect_failures >= 2:
                    # Handle likely expired/rejected — fall back to a fresh session
                    # that dives back in at the last highlighted segment.
                    state["handle"] = None
                    state["ctx_lost"] = True
                if connect_failures > 4:
                    with contextlib.suppress(Exception):
                        await ws.send_text(json.dumps({"type": "error", "message": "Live session ended unexpectedly."}))
                    break
                await asyncio.sleep(RECONNECT_BACKOFFS[min(connect_failures - 1, len(RECONNECT_BACKOFFS) - 1)])
    except WebSocketDisconnect:
        pass
    except Exception as exc:  # noqa: BLE001
        logger.error("live-teach session error: %s", exc, exc_info=exc)
        try:
            await ws.send_text(json.dumps({"type": "error", "message": "Live session ended unexpectedly."}))
        except Exception:  # noqa: BLE001
            pass
    finally:
        # Remember the session for a short grace window so a page refresh or
        # network blip rejoins the SAME teacher (Google keeps handles ~2h).
        if state.get("handle"):
            _RESUME_HANDLES[resume_key] = (state["handle"], time.monotonic())
            if len(_RESUME_HANDLES) > 500:
                cutoff = time.monotonic() - RESUME_TTL_SECONDS
                for k in [k for k, v in _RESUME_HANDLES.items() if v[1] < cutoff]:
                    _RESUME_HANDLES.pop(k, None)
        end = datetime.now(timezone.utc)
        activity.endTime = end
        activity.duration = max(0, int((end - started).total_seconds()))
        activity.status = "completed"
        try:
            await activity.save()
        except Exception:  # noqa: BLE001
            pass
        try:
            await ws.close()
        except Exception:  # noqa: BLE001
            pass
