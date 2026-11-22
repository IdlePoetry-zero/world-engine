"""Conservative, model-free recognition for waypoint choice replies."""

from __future__ import annotations

import re
from typing import Literal


# Keep these as complete-message phrases. Do not expand this into keyword or
# substring matching: the caller may use a result to resume a durable journey.
_STOP_PHRASES = frozenset(
    {
        "我在驿站停留",
        "在驿站停留",
        "在此停留",
        "在这里停留",
        "留在这里",
        "我想停下来",
        "我想在此停留",
        "我想在这里停留",
        "我选择在此停留",
        "我决定在此停留",
        "请在此停留",
        "请让我在此停留",
        "麻烦让我在这里停留",
        "stay here",
        "stop here",
        "i want to stay here",
        "i want to stop here",
        "i would like to stay here",
        "i would like to stop here",
        "please stay here",
        "please let me stay here",
    }
)

_CONTINUE_PHRASES = frozenset(
    {
        "继续原行程",
        "按原计划继续",
        "照原行程继续",
        "继续赶路",
        "我想继续原行程",
        "我想继续赶路",
        "不在这里停留，继续赶路",
        "我不在这里停留，继续赶路",
        "不在这里停留,继续赶路",
        "我不在这里停留,继续赶路",
        "不在此停留，继续赶路",
        "请继续原行程",
        "请继续赶路",
        "麻烦继续赶路",
        "keep going",
        "continue the original itinerary",
        "continue the original route",
        "continue as planned",
        "i want to keep going",
        "i want to continue the original itinerary",
        "please keep going",
        "please continue the original itinerary",
    }
)

_QUESTION_MARKS = frozenset("?？¿")
_QUOTE_MARKS = frozenset("\"'`“”‘’「」『』《》〈〉〝〞〟")
_EDGE_PUNCTUATION = " \t\r\n,，。.!！…⋯;；:："


def explicit_waypoint_choice(text: str) -> Literal["stop", "continue"] | None:
    """Recognize one exact, explicit waypoint choice from a whole message.

    Questions and quoted text are intentionally rejected before punctuation
    cleanup, so neither can accidentally become a match for an allowlisted
    choice. Respectful wording is accepted only where its complete form is in
    the same fixed allowlist.
    """

    if not isinstance(text, str) or not text.strip():
        return None
    if any(mark in text for mark in _QUESTION_MARKS | _QUOTE_MARKS):
        return None

    phrase = re.sub(r"\s+", " ", text.casefold()).strip(_EDGE_PUNCTUATION)
    # The only supported internal punctuation is the enumerated comma in the
    # Chinese "not staying, keep going" choice. Normalize its surrounding
    # whitespace, while preserving comma style for exact allowlist matching.
    phrase = re.sub(r"\s*([,，])\s*", r"\1", phrase)

    if phrase in _STOP_PHRASES:
        return "stop"
    if phrase in _CONTINUE_PHRASES:
        return "continue"
    return None
