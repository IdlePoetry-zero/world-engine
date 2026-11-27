"""Public HTTP request contracts; identity always comes from the session."""

from __future__ import annotations

from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StrictInt, model_validator
from world_engine.adapters.models.contracts import Capability
from world_engine.application.action_drafts import CreditResolutionSelection, DraftControl
from world_engine.application.work_chat import RoutineWorkScheduleRequest
from world_engine.domain.contests import ContestAction
from world_engine.domain.travel_itinerary import MAX_TRAVEL_ITINERARY_LEGS


class RequestModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


Version = Annotated[str, Field(pattern=r"^(0|[1-9][0-9]{0,18})$")]
Speed = Literal["fast", "balanced", "patient"]
Depth = Literal["standard", "deep", "exhaustive"]


class QualityProfile(RequestModel):
    speed: Speed = "balanced"
    depth: Depth = "deep"


class MediaJobRequest(RequestModel):
    source_event_id: UUID
    plugin_id: str = Field(pattern=r"^[a-z][a-z0-9_.-]{0,79}$")
    kind: Literal["speech", "image"]
    audience: Literal["public", "participants", "author_only"]
    actor_id: UUID | None = None
    description: str = Field(min_length=1, max_length=24000)


class CreateWorldRequest(RequestModel):
    pack_id: str = Field(min_length=1, max_length=160)
    pack_version: str | None = Field(default=None, max_length=32, pattern=r"^[0-9]+\.[0-9]+\.[0-9]+$")
    title: str = Field(min_length=1, max_length=240)
    player_name: str = Field(min_length=1, max_length=240)
    seed: Annotated[int, Field(strict=True, ge=-(2**53 - 1), le=2**53 - 1)] | None = None
    population_mode: Literal["natural", "sustained"] = "natural"
    npc_autonomy_mode: Literal["off", "limited_v1", "diverse_v1"] = "off"


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
    draft: DraftControl | None = Field(default=None, exclude_if=lambda value: value is None)
    scene_observation: Literal[True] | None = Field(default=None, exclude_if=lambda value: value is None)
    case_anchor_id: UUID | None = Field(default=None, exclude_if=lambda value: value is None)
    scene_action: CreditResolutionSelection | None = Field(default=None, exclude_if=lambda value: value is None)

    @model_validator(mode="after")
    def validate_structured_message(self) -> MessageRequest:
        if self.scene_observation and (self.mode != "ask_gm" or self.draft is not None or self.case_anchor_id is not None):
            raise ValueError("Scene observation requires its own GM query.")
        if self.case_anchor_id is not None and (self.mode != "action" or self.draft is not None):
            raise ValueError("A case anchor requires a direct action.")
        if self.scene_action is not None and (
            self.mode != "action" or self.draft is None or self.draft.operation != "prepare"
            or self.scene_observation is not None or self.case_anchor_id is not None
        ):
            raise ValueError("A scene action requires a new action preview.")
        return self


class NarrativeRewriteRequest(RequestModel):
    style: Literal["concise", "literary"]


class BranchRequest(RequestModel):
    branch_id: UUID
    sequence: Version
    label: str = Field(min_length=1, max_length=160)


class ContinueAfterDeathRequest(RequestModel):
    branch_id: UUID
    previous_player_id: UUID
    new_player_id: UUID
    expected_state_version: Version


class ChallengeRequest(RequestModel):
    branch_id: UUID
    actor_id: UUID
    expected_state_version: Version


class ContestRequest(RequestModel):
    branch_id: UUID
    actor_id: UUID
    expected_state_version: Version
    action: ContestAction


class JobControlRequest(RequestModel):
    action: Literal["pause", "resume", "cancel"]


class JobDecisionRequest(RequestModel):
    request_id: UUID
    option_id: str = Field(min_length=1, max_length=120, pattern=r"^[a-zA-Z][a-zA-Z0-9_.-]*$")


class WorldAdvanceRequest(RequestModel):
    branch_id: UUID
    expected_state_version: Version
    duration_seconds: Annotated[int, Field(strict=True, gt=0, le=2**53 - 1)]
    profile: QualityProfile = Field(default_factory=QualityProfile)


class LongActionRequest(WorldAdvanceRequest):
    activity: Literal["wait", "rest", "travel", "study", "work"]
    route_id: UUID | None = None
    route_ids: tuple[UUID, ...] | None = Field(default=None, min_length=1, max_length=MAX_TRAVEL_ITINERARY_LEGS)
    itinerary_auto_care: bool = Field(default=False, strict=True)
    skill_id: str | None = Field(
        default=None,
        min_length=1,
        max_length=160,
        pattern=r"^[a-zA-Z][a-zA-Z0-9_.-]*$",
    )
    study_stop_safety_at_or_below: StrictInt | None = Field(default=None, ge=0, le=100)
    work_schedule: RoutineWorkScheduleRequest | None = None

    @model_validator(mode="after")
    def action_parameters_match(self) -> LongActionRequest:
        route_selection_count = int(self.route_id is not None) + int(self.route_ids is not None)
        if route_selection_count != int(self.activity == "travel"):
            raise ValueError("Travel requires one registered route or one ordered route list.")
        if self.itinerary_auto_care and self.route_ids is None:
            raise ValueError("Automatic travel care is only available for an ordered itinerary.")
        if (self.activity == "study") != (self.skill_id is not None):
            raise ValueError("A skill is required only for study.")
        if self.study_stop_safety_at_or_below is not None and self.activity != "study":
            raise ValueError("A study safety threshold is only valid for study.")
        if (self.activity == "work") != (self.work_schedule is not None):
            raise ValueError("A recurring shift schedule is required only for work.")
        return self


class ProviderRequest(RequestModel):
    kind: Literal["deepseek", "openai_compatible", "ollama", "anthropic"]
    name: str = Field(min_length=1, max_length=160)
    base_url: str = Field(min_length=1, max_length=2000)
    secret_ref: str | None = Field(default=None, max_length=240)
    enabled: bool = True
    default_model: str | None = Field(default=None, max_length=240)
    max_retries: int = Field(default=2, ge=0, le=2)
    capability_overrides: dict[Capability, bool | None] = Field(default_factory=dict)
    allow_insecure_http: bool = False
    ollama_context_tokens: StrictInt | None = Field(default=None, gt=0, le=2_147_483_647)

    @model_validator(mode="after")
    def ollama_context_is_provider_specific(self) -> ProviderRequest:
        if self.ollama_context_tokens is not None and self.kind != "ollama":
            raise ValueError("ollama_context_tokens is only valid for Ollama providers")
        return self


class RoleRouteFallback(RequestModel):
    provider_id: str = Field(min_length=1, max_length=160)
    model: str | None = Field(default=None, max_length=240)
    max_output_tokens: Annotated[int, Field(strict=True, ge=1, le=2_147_483_647)] | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )


class RoleRoute(RequestModel):
    provider_id: str | None = Field(default=None, max_length=160)
    model: str | None = Field(default=None, max_length=240)
    max_output_tokens: Annotated[int, Field(strict=True, ge=1, le=2_147_483_647)] | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )
    fallbacks: list[RoleRouteFallback] = Field(default_factory=list, max_length=3, exclude_if=lambda value: not value)

    @model_validator(mode="after")
    def validate_fallbacks(self) -> RoleRoute:
        if self.fallbacks and not self.provider_id:
            raise ValueError("Fallback providers require a primary provider")
        targets = [(self.provider_id, self.model)] + [(target.provider_id, target.model) for target in self.fallbacks]
        if len(targets) != len(set(targets)):
            raise ValueError("A role route cannot repeat a provider/model target")
        return self


class SettingsRequest(QualityProfile):
    routes: dict[str, RoleRoute] = Field(default_factory=dict)
    local_only: bool = False
    revision: Annotated[int, Field(strict=True, ge=0)] | None = None
