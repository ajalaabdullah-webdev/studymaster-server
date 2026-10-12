"""Turn a note's HTML into ordered, speakable segments.

Each segment maps 1:1 (in document order) to a top-level block the frontend can
highlight: headings, paragraphs, list items, and figures. The spoken text has
maths converted to words and images described naturally, so read-aloud sounds
like a teacher walking through the note — and read-along can highlight + scroll
to the block currently being spoken.
"""

from __future__ import annotations

import html as _html
import re
from html.parser import HTMLParser

from app.services.math_speech import speak_inline_math, speak_residual

# Block tags that become one segment each (must match the frontend DOM walk:
# querySelectorAll('h1,h2,h3,p,li,figure')).
BLOCK_TAGS = {"h1", "h2", "h3", "p", "li", "figure"}
_KIND = {"h1": "heading", "h2": "heading", "h3": "subheading", "p": "text", "li": "list", "figure": "image"}


class _Segmenter(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.segments: list[dict] = []
        self._depth = 0  # block nesting depth
        self._tag: str | None = None
        self._buf: list[str] = []
        self._figcaption = False
        self._cap: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag in BLOCK_TAGS:
            if self._tag is None:
                self._tag = tag
                self._buf = []
                self._depth = 1
            else:
                self._depth += 1  # nested block; keep collecting under current
        if tag == "figcaption":
            self._figcaption = True
            self._cap = []

    def handle_endtag(self, tag):
        if tag == "figcaption":
            self._figcaption = False
        if tag in BLOCK_TAGS and self._tag is not None:
            self._depth -= 1
            if self._depth == 0:
                self._emit(self._tag)
                self._tag = None
                self._buf = []
                self._cap = []

    def handle_data(self, data):
        if self._figcaption:
            self._cap.append(data)
        elif self._tag is not None and not (self._tag == "figure"):
            self._buf.append(data)

    def _emit(self, tag: str) -> None:
        kind = _KIND.get(tag, "text")
        if tag == "figure":
            desc = _clean(" ".join(self._cap))
            spoken = f"Now, take a look at this image. {desc}" if desc else "Here is an illustration to help you picture this."
        else:
            raw = _clean(" ".join(self._buf))
            if not raw:
                return
            spoken = _to_speech(raw)
            if kind == "heading":
                spoken = f"{spoken}."
            elif kind == "subheading":
                spoken = f"{spoken}."
        spoken = _ensure_end(spoken)
        if spoken.strip():
            self.segments.append({"index": len(self.segments), "kind": kind, "spoken": spoken})


def _clean(s: str) -> str:
    return re.sub(r"\s+", " ", _html.unescape(s or "")).strip()


def _to_speech(text: str) -> str:
    text = speak_inline_math(text)  # $...$ spans
    text = speak_residual(text)     # bare \frac, ^, _, \commands
    return re.sub(r"\s+", " ", text).strip()


def _ensure_end(s: str) -> str:
    s = s.strip()
    if s and s[-1] not in ".!?:":
        s += "."
    return s


def segment_note(html_content: str) -> list[dict]:
    p = _Segmenter()
    try:
        p.feed(html_content or "")
        p.close()
    except Exception:  # noqa: BLE001
        pass
    return p.segments


def note_to_speech(html_content: str) -> str:
    """Full spoken transcript (segments joined with sentence pauses)."""
    segs = segment_note(html_content)
    return "  ".join(s["spoken"] for s in segs)
