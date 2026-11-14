"""Public HTTP request contracts; identity always comes from the session."""

from __future__ import annotations

from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class RequestModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


Version = Annotated[str, Field(pattern=r"^(0|[1-9][0-9]{0,18})$")]
Speed = Literal["fast", "balanced", "patient"]
Depth = Literal["standard", "deep", "exhaustive"]


class QualityProfile(RequestModel):
    speed: Speed = "balanced"
    depth: Depth = "deep"


class CreateWorldRequest(RequestModel):
    pack_id: str = Field(min_length=1, max_length=160)
    title: str = Field(min_length=1, max_length=240)
    player_name: str = Field(min_length=1, max_length=240)


class MessageRequest(RequestModel):
    schema_version: Literal["1.0"] = "1.0"
    request_id: UUID
    world_id: UUID
    branch_id: UUID
    session_id: UUID
    actor_id: UUID
    expected_state_version: Version
    mode: Literal["auto", "ask_gm", "roleplay", "action", "author"] = "auto"
    text: str = Field(min_length=1, max_length=100_000)
    profile: QualityProfile = Field(default_factory=QualityProfile)


class BranchRequest(RequestModel):
    checkpoint: UUID
    label: str = Field(min_length=1, max_length=160)


class JobControlRequest(RequestModel):
    action: Literal["pause", "resume", "cancel"]


class ProviderRequest(RequestModel):
    kind: Literal["deepseek", "openai_compatible", "ollama", "anthropic"]
    name: str = Field(min_length=1, max_length=160)
    base_url: str = Field(min_length=1, max_length=2000)
    secret_ref: str | None = Field(default=None, max_length=240)
    enabled: bool = True
    default_model: str | None = Field(default=None, max_length=240)


class RoleRoute(RequestModel):
    provider_id: str | None = Field(default=None, max_length=160)
    model: str | None = Field(default=None, max_length=240)


class SettingsRequest(QualityProfile):
    routes: dict[str, RoleRoute] = Field(default_factory=dict)
    local_only: bool = False
