"""Live Teaching — a real-time Gemini Live session that teaches a note aloud.

Architecture: the browser talks to OUR WebSocket; we bridge to Gemini Live
server-side (API key never leaves the backend, and we inject the note as
context). Audio flows both ways as PCM chunks; the model calls a
``show_segment`` tool before teaching each part, which we forward to the client
so the on-screen highlight follows the voice. Barge-in (the student holding the
mic and talking) uses Gemini Live's native interruption.
"""

from __future__ import annotations

import logging
from typing import Any

from google.genai import types

from app.models.note import Note
from app.services.note_speech import segment_note

logger = logging.getLogger("studymaster.live_teach")

SHOW_SEGMENT_TOOL = types.Tool(
    function_declarations=[
        types.FunctionDeclaration(
            name="show_segment",
            # Fire-and-forget: native-audio Live models abort the stream (1006/1007)
            # if a BLOCKING tool call lands mid-audio. NON_BLOCKING + a SILENT
            # response lets the model keep speaking while we move the highlight.
            behavior=types.Behavior.NON_BLOCKING,
            description=(
                "Highlight the part of the note you are about to teach or revisit. "
                "ALWAYS call this right before you start speaking about a segment, "
                "including when the student asks you to go back to an earlier part."
            ),
            parameters=types.Schema(
                type=types.Type.OBJECT,
                properties={
                    "index": types.Schema(
                        type=types.Type.INTEGER,
                        description="The segment number to highlight (from the numbered note segments).",
                    )
                },
                required=["index"],
            ),
        )
    ]
)


def build_segments(note: Note) -> list[dict[str, Any]]:
    """The same ordered segments the read-along uses (index ↔ on-screen block)."""
    return segment_note(note.content)


def build_config(
    note: Note,
    segments: list[dict[str, Any]],
    resume_handle: str | None = None,
) -> types.LiveConnectConfig:
    seg_lines = []
    for s in segments:
        kind = s.get("kind", "text")
        prefix = "[IMAGE] " if kind == "image" else ""
        seg_lines.append(f"{s['index']}: {prefix}{s['spoken']}")
    seg_text = "\n".join(seg_lines)

    system = f"""You are a warm, patient NIGERIAN teacher on StudyMaster, teaching a student their study note
LIVE, one part at a time. You speak with a natural Nigerian accent and cadence — use Nigerian English
expressions naturally (e.g. "you see", "not so?", "well done!"), stay encouraging, and keep each
explanation short and clear before moving on.

THE NOTE, split into numbered segments (segment number: content):
{seg_text}

HOW TO TEACH:
1. Start by briefly welcoming the student and telling them what the note covers, then teach segment by
   segment IN ORDER, starting from segment 0.
2. ALWAYS call show_segment with the segment number RIGHT BEFORE you speak about that segment — never
   speak about a segment without calling it first. This highlights it on the student's screen.
3. Don't just read the text robotically — teach it: rephrase simply, add a quick everyday Nigerian
   example where it helps, and check in briefly ("are you with me?") between major sections.
4. Segments marked [IMAGE] are pictures in the note — when you reach one, call show_segment for it and
   describe what the image shows and why it matters, as if you can both see it.
5. If the student interrupts to ask something: answer it warmly and simply. If they ask you to go back
   to an earlier part, call show_segment for that part and re-explain it a DIFFERENT, simpler way.
6. After answering, continue from where you left off (call show_segment again for the current part).
7. You are ONLY a teacher — you cannot edit or rewrite the note. If asked to change the note, kindly
   say they can use the note assistant panel for edits.
8. When you finish the final segment, give a short, warm summary and encourage them to try the quiz.

READING FORMULAS:
- Mathematical formulas in the segments are already written out in words (e.g. "x squared plus two x
  plus one equals zero"). Read them SLOWLY and naturally, like a teacher at a board — pause briefly
  between terms, and after reading a formula, say in one short sentence what it means.

WHEN THE STUDENT PRESSES THE MIC:
- Respond ONLY to what you clearly heard them say.
- If their audio was silent, noisy, or unclear, say exactly this kind of thing: "Sorry, I didn't hear
  you well — hold the button and ask me again." Then CONTINUE teaching from where you stopped.
- NEVER invent, assume, or imagine a question the student did not ask. Never answer a question you
  did not clearly hear.
- Stay strictly within this note and its subject. If asked something unrelated, gently bring the
  student back to the note.

IF THE SESSION EVER RESUMES AFTER A PAUSE OR RECONNECTION:
- Continue teaching EXACTLY where you stopped — mid-sentence if needed.
- NEVER greet the student again, never say "welcome back", never apologise for a pause the
  student may not have noticed, and never restart or re-summarise what you already taught.

PACING — THIS IS IMPORTANT:
- Teach ONE segment per turn. Call show_segment, teach that single segment, then STOP
  speaking and end your turn. Wait to be told to continue.
- Do NOT run ahead through several segments in one turn, and do NOT ask "should I
  continue?" — simply stop after each segment; the student's app will cue you.

Keep individual speaking turns reasonably short so the student can follow along."""

    return types.LiveConnectConfig(
        response_modalities=[types.Modality.AUDIO],
        system_instruction=types.Content(parts=[types.Part(text=system)]),
        tools=[SHOW_SEGMENT_TOOL],
        speech_config=types.SpeechConfig(
            voice_config=types.VoiceConfig(
                prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name="Aoede")
            )
        ),
        # Push-to-talk: WE signal when the student starts/stops speaking
        # (activity_start/activity_end), instead of automatic voice detection —
        # which needs a continuous stream and misses short hold-to-talk bursts.
        realtime_input_config=types.RealtimeInputConfig(
            automatic_activity_detection=types.AutomaticActivityDetection(disabled=True)
        ),
        # Google keeps the session state server-side; a resumption handle lets a NEW
        # websocket continue the SAME lesson (same context, no re-greeting). Passing
        # handle=None still turns on the periodic SessionResumptionUpdate messages.
        session_resumption=types.SessionResumptionConfig(handle=resume_handle),
        # Without compression, audio sessions are hard-capped at ~15 minutes. The
        # sliding window discards oldest turns but NEVER the system instruction, so
        # the note itself survives for the whole lesson.
        context_window_compression=types.ContextWindowCompressionConfig(
            sliding_window=types.SlidingWindow()
        ),
    )
