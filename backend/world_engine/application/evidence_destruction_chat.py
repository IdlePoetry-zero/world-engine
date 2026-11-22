"""Bind an irreversible evidence action to the source named in chat."""

import re
import unicodedata
from uuid import UUID


_DESTROY = r"(?:销毁|焚毁|毁掉|烧掉)"
_DIRECT_DESTRUCTION = re.compile(
    rf"(?:请(?:你|您)?\s*)?(?:帮我|替我|我(?:现在)?(?:要|决定)?)?\s*(?:现在\s*)?"
    rf"(?:{_DESTROY}\s*(?P<after>.+)|把\s*(?P<before>.+?)\s*(?:彻底\s*)?{_DESTROY})"
)
_TITLE_QUOTES = (("「", "」"), ("『", "』"), ("《", "》"), ("“", "”"), ('"', '"'), ("'", "'"))


def evidence_destruction_matches(text: str, target_id: UUID, sources: list[dict[str, str | None]]) -> bool:
    normalized = unicodedata.normalize("NFKC", text).strip().rstrip(".。! ").casefold()
    command = _DIRECT_DESTRUCTION.fullmatch(normalized)
    if command is None:
        return False
    title = (command.group("after") or command.group("before")).strip()
    for left, right in _TITLE_QUOTES:
        if title.startswith(left) and title.endswith(right):
            title = title[1:-1].strip()
            break
    named_source_ids = {
        source["target_id"] for source in sources
        if title in {
            source["target_id"],
            unicodedata.normalize("NFKC", source["name"] or "").strip().casefold(),
        }
    }
    return bool(title) and named_source_ids == {str(target_id)}
