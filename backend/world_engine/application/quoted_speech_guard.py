"""Narrow one-way guard for fully framed in-character quoted speech.

Complete speech frames can be routed before automatic intent classification.
The guard never constructs an action intent or extracts quoted text for
execution. Target checks share the dialogue dispatcher's visibility projection.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import re
from typing import Literal
from uuid import UUID

from world_engine.agents import AgentVisibleEntity, build_agent_knowledge
from world_engine.domain.models import WorldState

from .visibility import present_dialogue_targets, project_player_state


_TARGET = r"[\u3400-\u9fffA-Za-z0-9·_-]{1,24}"
_SPEECH = r"「(?P<quote>[^「」\r\n]{1,600})」"
_PLAYER_TO_NPC = re.compile(
    rf"^[ \t]*我[ \t]*(?:对|向)[ \t]*(?P<target>{_TARGET})[ \t]*(?:说(?:道)?|问)[ \t]*[：:]"
    rf"[ \t]*{_SPEECH}[ \t]*[。！？!?…]*[ \t]*$"
)
_NPC_REPORTED_SPEECH = re.compile(
    rf"^[ \t]*(?!我|角色)(?P<target>{_TARGET})[ \t]*说(?:道)?[ \t]*[：:]"
    rf"[ \t]*{_SPEECH}[ \t]*[。！？!?…]*[ \t]*$"
)
_UNTRUSTED_WRAPPERS = (
    "[untrusted]", "[/untrusted]", "[[untrusted]]", "[[/untrusted]]",
    "<untrusted>", "</untrusted>", "```", "<|system|>", "<|assistant|>",
)
_CONTROL_ADDRESSEES = frozenset({
    "gm", "主持人", "主持", "作者", "编剧", "系统", "世界引擎", "规则引擎", "设定编辑器",
})
_UNBOUND_TARGETS = frozenset({
    "我", "你", "您", "他", "她", "它", "我们", "你们", "他们", "她们", "它们",
    "对方", "那个人", "某人", "某角色", "角色",
})


@dataclass(frozen=True, slots=True)
class QuotedSpeechFrame:
    kind: Literal["player_to_npc", "npc_reported_speech"]
    target: str
    target_id: str | None = None


def visible_dialogue_targets(state: WorldState, actor_id: UUID) -> dict[str, AgentVisibleEntity]:
    """Use the same authoritative scene projection as ordinary dialogue."""
    observation = build_agent_knowledge(state, actor_id)
    visible = project_player_state(state.model_dump(mode="json"), str(actor_id))["entities"]
    return present_dialogue_targets(state, observation, visible)


def _syntactic_frame(text: str) -> QuotedSpeechFrame | None:
    if not text or len(text) > 1_000 or "\n" in text or "\r" in text:
        return None
    folded = text.casefold()
    if any(marker in folded for marker in _UNTRUSTED_WRAPPERS):
        return None
    for pattern, kind in (
        (_PLAYER_TO_NPC, "player_to_npc"),
        (_NPC_REPORTED_SPEECH, "npc_reported_speech"),
    ):
        match = pattern.fullmatch(text)
        if match is None:
            continue
        target = match.group("target")
        folded_target = target.casefold()
        if folded_target in _CONTROL_ADDRESSEES:
            return None
        if not match.group("quote").strip():
            return None
        # Only the addressee label is retained for visibility checks. The quote
        # body is intentionally never returned or passed to an action.
        return QuotedSpeechFrame(
            kind=kind,
            target=target,
        )
    return None


def match_quoted_speech_frame(
    state: WorldState,
    actor_id: UUID,
    text: str,
    *,
    present_targets: Mapping[str, AgentVisibleEntity] | None = None,
) -> QuotedSpeechFrame | None:
    """Recognize one safe speech frame and report whether its target grounds.

    A complete player-to-NPC frame remains recognizable when its target is
    absent, hidden, duplicated, or an unbound pronoun. Callers can then clarify
    without dispatching an action or revealing which target check failed.
    Reported NPC speech needs no target projection: it is never executable as
    the player's request and is clarified whenever the frame itself is safe.
    """
    frame = _syntactic_frame(text)
    if frame is None:
        return None
    if frame.kind == "npc_reported_speech":
        return frame

    if frame.target.casefold() in _UNBOUND_TARGETS:
        return frame
    targets = present_targets if present_targets is not None else visible_dialogue_targets(state, actor_id)
    matched = [ident for ident, entity in targets.items() if frame.target in {entity.name, entity.public_role}]
    return QuotedSpeechFrame(kind=frame.kind, target=frame.target, target_id=matched[0] if len(matched) == 1 else None)
