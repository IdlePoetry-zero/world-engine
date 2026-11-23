"""Strict, read-only NPC action capability preflight.

This module compiles persisted NPC plan steps into existing rule intents and
asks the same :class:`RuleEngine` used for player actions to evaluate them. It
does not apply events, advance time, or grant a plan additional authority.
Long actions are returned as proposals for a shared simulation host to
schedule at an interval boundary.
"""

from __future__ import annotations

from collections.abc import Callable, Collection
from dataclasses import dataclass
import re
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ValidationError

from world_engine.domain.components import MarketComponent, PlaceComponent, ProductionComponent, SkillsComponent
from world_engine.domain.investigation import entity_visible_to_actor
from world_engine.domain.models import DomainEvent, WorldEntity, WorldState
from world_engine.domain.npc_plans import (
    NPC_EXPLORATION_TICKS, NPC_SHARED_EXPLORATION_POLICY, NPC_SHARED_PRODUCTION_POLICY,
    NPC_SHARED_TRADE_POLICY, NPC_SHARED_TRAINING_POLICY,
    NPC_TRADE_TICKS, NpcPlanStep, occupied_npc_ids,
)
from world_engine.domain.reducers import state_hash
from world_engine.rules.contracts import (
    ACTION_INTENT_ADAPTER,
    CastLeadershipBallotVoteIntent,
    ExploreIntent,
    FulfillReturnPromiseIntent,
    IntentModel,
    MoveIntent,
    ProduceIntent,
    ProposeRelationshipInvitationIntent,
    ResolveRelationshipInvitationIntent,
    RuleContext,
    RuleOutcome,
    RuleResolution,
    SpeakIntent,
    TradeIntent,
    TrainIntent,
)
from world_engine.rules.engine import RuleEngine


CapabilityStatus = Literal["rejected", "instant", "journey_start", "interval_required"]
_CAPABILITY_ID_PATTERN = re.compile(r"^[a-zA-Z][a-zA-Z0-9_.-]{0,159}$")
_CHARACTER_KINDS = frozenset({"character", "person"})
_PLACE_KINDS = frozenset({"place", "location"})


class DuplicateNpcCapabilityError(ValueError):
    """Raised when a capability identifier is registered more than once."""


ReferenceValidator = Callable[[WorldState, WorldEntity, IntentModel], tuple[str, str] | None]
IntentAdapter = Callable[[IntentModel], object]


@dataclass(frozen=True, slots=True)
class NpcCapability:
    """One registered, strict mapping from plan parameters to a rule action."""

    capability_id: str
    action_kind: str
    intent_model: type[IntentModel]
    adapter: IntentAdapter
    reference_validator: ReferenceValidator | None = None


class NpcCapabilityRegistry:
    """Explicit registry for NPC capabilities; it contains no execution code."""

    def __init__(self, *, registry_version: str = "npc-capabilities.v1") -> None:
        if not registry_version or len(registry_version) > 100:
            raise ValueError("registry_version must be a bounded version identifier")
        self.registry_version = registry_version
        self._capabilities: dict[str, NpcCapability] = {}

    def register(
        self,
        capability_id: str,
        intent_model: type[IntentModel],
        *,
        adapter: IntentAdapter | None = None,
        reference_validator: ReferenceValidator | None = None,
    ) -> NpcCapability:
        """Register a strict action schema and optional explicit adapter.

        A custom adapter may reshape that schema into an action already
        accepted by ``ACTION_INTENT_ADAPTER``. It cannot introduce a new event
        path or bypass the closed rule-intent union. Request/host-solicitation
        actions are intentionally not available to NPC plans.
        """
        if not _CAPABILITY_ID_PATTERN.fullmatch(capability_id):
            raise ValueError("capability_id is not a valid bounded identifier")
        if capability_id in self._capabilities:
            raise DuplicateNpcCapabilityError(f"NPC capability {capability_id!r} is already registered")
        if not isinstance(intent_model, type) or not issubclass(intent_model, IntentModel):
            raise TypeError("intent_model must be a strict IntentModel subclass")
        if intent_model.__name__.startswith("Request"):
            raise ValueError("request/host-solicitation intents cannot be registered as NPC capabilities")
        kind_field = intent_model.model_fields.get("kind")
        action_kind = getattr(kind_field, "default", None)
        if not isinstance(action_kind, str) or action_kind.startswith("request_"):
            raise ValueError("intent_model must declare one non-request action kind")
        capability = NpcCapability(
            capability_id=capability_id,
            action_kind=action_kind,
            intent_model=intent_model,
            adapter=adapter or (lambda parsed: parsed),
            reference_validator=reference_validator,
        )
        self._capabilities[capability_id] = capability
        return capability

    def get(self, capability_id: str) -> NpcCapability | None:
        return self._capabilities.get(capability_id)

    def __contains__(self, capability_id: str) -> bool:
        return capability_id in self._capabilities

    def __iter__(self):
        return iter(self._capabilities.values())


@dataclass(frozen=True, slots=True)
class NpcCapabilityPreflight:
    """An unevaluated-by-host proposal plus the pure rule resolution."""

    status: CapabilityStatus
    capability_id: str
    action_kind: str | None
    registry_version: str
    ruleset_version: str
    resolution: RuleResolution
    required_ticks: int | None
    state_hash_before: str
    state_hash_after: str
    committed: Literal[False] = False

    @property
    def proposed_events(self) -> tuple[DomainEvent, ...]:
        """All original rule events, including timing events, without filtering."""
        return tuple(self.resolution.events)

    @property
    def world_mutated(self) -> Literal[False]:
        return False


def capability_id_for_action(action_kind: str) -> str:
    """Return the stable default identifier for an existing rule action."""
    if not re.fullmatch(r"[a-z][a-z0-9_]{0,79}", action_kind):
        raise ValueError("action_kind must be a registered-style lower-case action name")
    return f"world.action.{action_kind}.v1"


def build_default_npc_capability_registry() -> NpcCapabilityRegistry:
    """Build a fresh, explicit allowlist of the currently preflightable actions."""
    registry = NpcCapabilityRegistry()
    registry.register(capability_id_for_action("move"), MoveIntent, reference_validator=_validate_move_reference)
    registry.register(capability_id_for_action("explore"), ExploreIntent, reference_validator=_validate_explore_reference)
    registry.register(capability_id_for_action("train"), TrainIntent, reference_validator=_validate_train_reference)
    registry.register(capability_id_for_action("trade"), TradeIntent, reference_validator=_validate_trade_reference)
    registry.register(capability_id_for_action("produce"), ProduceIntent, reference_validator=_validate_produce_reference)
    registry.register(capability_id_for_action("speak"), SpeakIntent, reference_validator=_validate_speak_reference)
    registry.register(
        capability_id_for_action("fulfill_return_promise"),
        FulfillReturnPromiseIntent,
        reference_validator=_validate_return_promise_reference,
    )
    registry.register(
        capability_id_for_action("propose_relationship_invitation"),
        ProposeRelationshipInvitationIntent,
        reference_validator=_validate_relationship_invitation_reference,
    )
    registry.register(
        capability_id_for_action("resolve_relationship_invitation"),
        ResolveRelationshipInvitationIntent,
        reference_validator=_validate_relationship_invitation_reference,
    )
    registry.register(
        capability_id_for_action("cast_leadership_ballot_vote"),
        CastLeadershipBallotVoteIntent,
        reference_validator=_validate_ballot_vote_reference,
    )
    return registry


def preflight_npc_plan_step(
    state: WorldState,
    actor_id: UUID,
    step: NpcPlanStep,
    context: RuleContext,
    *,
    rule_engine: RuleEngine | None = None,
    registry: NpcCapabilityRegistry | None = None,
    externally_controlled_actor_ids: Collection[UUID] = (),
) -> NpcCapabilityPreflight:
    """Validate and evaluate one NPC plan step without changing ``state``.

    The returned events are proposals only. A host must revalidate against its
    current branch and use the shared time coordinator before committing any
    duration-bearing action.
    """
    active_registry = registry or build_default_npc_capability_registry()
    engine = rule_engine or RuleEngine()
    before = state_hash(state)
    capability = active_registry.get(step.capability_id)

    def finish(
        status: CapabilityStatus,
        resolution: RuleResolution,
        *,
        action_kind: str | None = None,
        required_ticks: int | None = None,
    ) -> NpcCapabilityPreflight:
        return NpcCapabilityPreflight(
            status=status,
            capability_id=step.capability_id,
            action_kind=action_kind,
            registry_version=active_registry.registry_version,
            ruleset_version=context.ruleset_version,
            resolution=resolution,
            required_ticks=required_ticks,
            state_hash_before=before,
            state_hash_after=state_hash(state),
        )

    def reject(code: str, message: str, *, action_kind: str | None = None) -> NpcCapabilityPreflight:
        resolution = RuleResolution(
            events=[],
            outcome=RuleOutcome(status="rejected", code=code, message=message),
            elapsed_ticks=0,
            ruleset_version=context.ruleset_version,
        )
        return finish("rejected", resolution, action_kind=action_kind)

    if context.actor_id != actor_id:
        return reject("npc_actor_context_mismatch", "The rule context does not name the requested NPC actor.")
    if actor_id in externally_controlled_actor_ids:
        return reject("npc_actor_externally_controlled", "A user-controlled character must act for themselves.")
    if actor_id == state.player_id:
        return reject("npc_actor_is_player", "The player character cannot be acted for by NPC automation.")
    actor = state.entities.get(actor_id)
    if actor is None or actor.kind not in _CHARACTER_KINDS or actor.status != "active":
        return reject("npc_actor_unavailable", "The requested NPC actor is not an active person in this world.")
    if capability is None:
        return reject("unsupported_npc_capability", "The plan step does not name a registered NPC capability.")
    if (
        context.include_actor_in_background
        or context.background_routine_mode
        or context.background_choices
        or context.accusation_repair_requested_by is not None
        or context.accusation_repair_trust_proof is not None
    ):
        return reject("npc_rule_context_not_direct", "NPC capability preflight accepts only a direct rule-action context.")

    try:
        raw_arguments = dict(step.arguments)
        supplied_kind = raw_arguments.get("kind")
        if supplied_kind is not None and supplied_kind != capability.action_kind:
            raise ValueError("The supplied action kind does not match this capability identifier.")
        parsed_parameters = capability.intent_model.model_validate(raw_arguments)
        adapted = capability.adapter(parsed_parameters)
        if isinstance(adapted, BaseModel):
            action_payload: object = adapted.model_dump(mode="python", exclude_none=False)
        else:
            action_payload = adapted
        action = ACTION_INTENT_ADAPTER.validate_python(action_payload)
        if action.kind != capability.action_kind:
            raise ValueError("The capability adapter produced a different action kind.")
    except (ValidationError, TypeError, ValueError):
        return reject(
            "invalid_npc_action_arguments",
            f"Action arguments do not match the registered {capability.action_kind!r} schema or adapter.",
            action_kind=capability.action_kind,
        )

    if capability.reference_validator is not None:
        reference_issue = capability.reference_validator(state, actor, action)
        if reference_issue is not None:
            return reject(reference_issue[0], reference_issue[1], action_kind=capability.action_kind)

    if isinstance(action, ExploreIntent) and state.metadata.get(NPC_SHARED_EXPLORATION_POLICY) is True:
        if actor_id in occupied_npc_ids(state, state.current_tick) or any(row.actor_id == actor_id for row in state.npc_attendance):
            return reject("npc_actor_busy", "The character is already searching.", action_kind=action.kind)
        if state.current_tick > 2**63 - 1 - NPC_EXPLORATION_TICKS:
            return reject("tick_overflow", "The search exceeds the world clock range.", action_kind=action.kind)
        return finish(
            "interval_required", RuleResolution(
                outcome=RuleOutcome(status="succeeded", code="exploration_available", message="A search can begin here."),
                ruleset_version=context.ruleset_version,
            ), action_kind=action.kind, required_ticks=NPC_EXPLORATION_TICKS,
        )

    if isinstance(action, TradeIntent) and state.metadata.get(NPC_SHARED_TRADE_POLICY) is True:
        if actor_id in occupied_npc_ids(state, state.current_tick) or any(row.actor_id == actor_id for row in state.npc_attendance):
            return reject("npc_actor_busy", "The character already has an action in progress.", action_kind=action.kind)
        if state.current_tick > 2**63 - 1 - NPC_TRADE_TICKS:
            return reject("tick_overflow", "The trade exceeds the world clock range.", action_kind=action.kind)
        quote = engine._trade_settlement(state, actor, action, context)
        if quote.outcome.status == "rejected":
            return finish("rejected", quote, action_kind=action.kind)
        return finish("interval_required", quote, action_kind=action.kind, required_ticks=NPC_TRADE_TICKS)

    shared_training = isinstance(action, TrainIntent) and state.metadata.get(NPC_SHARED_TRAINING_POLICY) is True
    shared_production = isinstance(action, ProduceIntent) and state.metadata.get(NPC_SHARED_PRODUCTION_POLICY) is True
    if shared_training or shared_production:
        if (
            actor_id in occupied_npc_ids(state, state.current_tick)
            or any(row.actor_id == actor_id for row in state.npc_attendance)
            or any(row.actor_id == actor_id and row.status == "in_transit" for row in state.travel_journeys)
        ):
            return reject("npc_actor_busy", "The character already has an action in progress.", action_kind=action.kind)
        place = state.entities.get(actor.location_id)
        if place is not None and place.status != "active":
            return reject("work_location_unavailable", "The action location is unavailable.", action_kind=action.kind)
        if isinstance(action, TrainIntent):
            duration = action.duration_ticks
            due_tick = state.current_tick + duration
            if due_tick > 2**63 - 1:
                return reject("tick_overflow", "Training exceeds the world clock range.", action_kind=action.kind)
            progress = engine._training_progress(actor, action, context, trained_at_tick=due_tick)
        else:
            progress = engine._production_settlement(state, actor, action, context)
            duration = progress.outcome.details.get("duration_seconds")
        if progress.outcome.status == "rejected":
            return finish("rejected", progress, action_kind=action.kind)
        if not isinstance(duration, int) or duration <= 0 or state.current_tick + duration > 2**63 - 1:
            return reject("tick_overflow", "The action exceeds the world clock range.", action_kind=action.kind)
        return finish("interval_required", progress, action_kind=action.kind, required_ticks=duration)

    if isinstance(action, MoveIntent):
        # Mirror RuleEngine._move route choice exactly: earliest travel time,
        # then lexical route UUID. The chosen route is handed to the dedicated
        # departure evaluator, which records only a journey start.
        route = _selected_move_route(state, actor, action)
        if route is None:
            return reject("npc_reference_not_known", "That route is not available from the actor's current place.", action_kind=action.kind)
        resolution = engine.evaluate_travel_departure(state, route.route_id, context)
        if resolution.outcome.status != "succeeded":
            return finish("rejected", resolution, action_kind=action.kind)
        if resolution.ruleset_version != context.ruleset_version:
            return reject("npc_ruleset_version_mismatch", "The rule engine returned a different ruleset version.", action_kind=action.kind)
        return finish("journey_start", resolution, action_kind=action.kind, required_ticks=route.travel_ticks)

    resolution = engine.evaluate(state, action, context)
    if resolution.ruleset_version != context.ruleset_version:
        return reject("npc_ruleset_version_mismatch", "The rule engine returned a different ruleset version.", action_kind=action.kind)
    if resolution.outcome.status == "rejected":
        return finish("rejected", resolution, action_kind=action.kind)
    if resolution.elapsed_ticks > 0:
        return finish(
            "interval_required",
            resolution,
            action_kind=action.kind,
            required_ticks=resolution.elapsed_ticks,
        )
    if resolution.outcome.status != "succeeded":
        return finish("rejected", resolution, action_kind=action.kind)
    return finish("instant", resolution, action_kind=action.kind)


def _selected_move_route(state: WorldState, actor: WorldEntity, intent: MoveIntent):
    if actor.location_id is None:
        return None
    origin = state.entities.get(actor.location_id)
    if origin is None:
        return None
    try:
        place = PlaceComponent.model_validate(origin.components.get("place"))
    except Exception:
        return None
    candidates = [
        route
        for route in place.routes
        if route.to_place_id == intent.destination_id
        and (intent.route_id is None or route.route_id == intent.route_id)
    ]
    return min(candidates, key=lambda item: (item.travel_ticks, str(item.route_id)), default=None)


def _validate_move_reference(
    state: WorldState, actor: WorldEntity, action: IntentModel,
) -> tuple[str, str] | None:
    assert isinstance(action, MoveIntent)
    route = _selected_move_route(state, actor, action)
    if route is None:
        return "npc_reference_not_known", "That route is not available from the actor's current place."
    destination = state.entities.get(action.destination_id)
    if destination is None or destination.kind not in _PLACE_KINDS or destination.status != "active":
        return "npc_reference_not_known", "That destination is not in the actor's available route knowledge."
    if destination.components.get("hidden") is True and not entity_visible_to_actor(state, actor.id, destination.id):
        return "npc_reference_not_known", "That destination is not in the actor's available route knowledge."
    return None


def _validate_speak_reference(
    state: WorldState, actor: WorldEntity, action: IntentModel,
) -> tuple[str, str] | None:
    assert isinstance(action, SpeakIntent)
    for target_id in action.target_ids:
        target = state.entities.get(target_id)
        if (
            target is None
            or target.id == actor.id
            or target.kind not in _CHARACTER_KINDS
            or target.status != "active"
            or actor.location_id is None
            or target.location_id != actor.location_id
            or not entity_visible_to_actor(state, actor.id, target.id)
            or not entity_visible_to_actor(state, target.id, actor.id)
        ):
            return "npc_reference_not_known", "Speech must name only other active people visible in the current scene."
    return None


def _validate_explore_reference(
    state: WorldState, actor: WorldEntity, action: IntentModel,
) -> tuple[str, str] | None:
    assert isinstance(action, ExploreIntent)
    place = state.entities.get(action.place_id)
    if (
        place is None
        or place.kind not in _PLACE_KINDS
        or place.status != "active"
        or actor.location_id != place.id
        or not entity_visible_to_actor(state, actor.id, place.id)
    ):
        return "npc_reference_not_known", "Exploration must refer to the actor's current visible place."
    return None


def _validate_train_reference(
    state: WorldState, actor: WorldEntity, action: IntentModel,
) -> tuple[str, str] | None:
    assert isinstance(action, TrainIntent)
    try:
        skills = SkillsComponent.model_validate(actor.components.get("skills"))
    except Exception:
        return "npc_reference_not_known", "Training must refer to a skill in the actor's own skill record."
    if action.skill_id not in skills.skills:
        return "npc_reference_not_known", "Training must refer to a skill in the actor's own skill record."
    return None


def _validate_trade_reference(
    state: WorldState, actor: WorldEntity, action: IntentModel,
) -> tuple[str, str] | None:
    assert isinstance(action, TradeIntent)
    market_entity = state.entities.get(actor.location_id) if actor.location_id is not None else None
    if market_entity is None or market_entity.status != "active" or not entity_visible_to_actor(state, actor.id, market_entity.id):
        return "npc_reference_not_known", "Trade must refer to a listing at the actor's current visible market."
    try:
        market = MarketComponent.model_validate(market_entity.components.get("market"))
    except Exception:
        return "npc_reference_not_known", "Trade must refer to a listing at the actor's current visible market."
    offer = next((item for item in market.offers if item.listing_id == action.listing_id and item.active), None)
    seller = state.entities.get(offer.seller_id) if offer is not None else None
    if (
        offer is None
        or seller is None
        or seller.status != "active"
        or seller.location_id != market_entity.id
        or not entity_visible_to_actor(state, actor.id, seller.id)
    ):
        return "npc_reference_not_known", "Trade must refer to a listing at the actor's current visible market."
    return None


def _validate_produce_reference(
    state: WorldState, actor: WorldEntity, action: IntentModel,
) -> tuple[str, str] | None:
    assert isinstance(action, ProduceIntent)
    producer = state.entities.get(action.producer_id)
    colocated = producer is not None and (
        producer.id == actor.id
        or actor.location_id == producer.id
        or producer.location_id is not None and producer.location_id == actor.location_id
    )
    if (
        producer is None
        or producer.status != "active"
        or not colocated
        or producer.id != actor.id and not entity_visible_to_actor(state, actor.id, producer.id)
    ):
        return "npc_reference_not_known", "Production must refer to the actor or a co-located visible producer."
    try:
        production = ProductionComponent.model_validate(producer.components.get("production"))
    except Exception:
        return "npc_reference_not_known", "Production must refer to a recipe in the visible producer's record."
    if not any(recipe.recipe_id == action.recipe_id for recipe in production.recipes):
        return "npc_reference_not_known", "Production must refer to a recipe in the visible producer's record."
    return None


def _validate_return_promise_reference(
    state: WorldState, actor: WorldEntity, action: IntentModel,
) -> tuple[str, str] | None:
    assert isinstance(action, FulfillReturnPromiseIntent)
    promise = next((item for item in state.relationship_promises if item.id == action.promise_id
                    and item.promisor_id == actor.id and item.status == "active"), None)
    if promise is None:
        return "npc_reference_not_known", "A return must refer to the actor's own active promise."
    if not entity_visible_to_actor(state, actor.id, promise.promisee_id):
        return "promise_recipient_not_observed", "The recipient is not visible at the actor's current location."
    return None


def _validate_relationship_invitation_reference(
    state: WorldState, actor: WorldEntity, action: IntentModel,
) -> tuple[str, str] | None:
    if isinstance(action, ProposeRelationshipInvitationIntent):
        target = state.entities.get(action.target_id)
        if (
            target is None
            or target.kind not in _CHARACTER_KINDS
            or target.status != "active"
            or actor.location_id is None
            or target.location_id != actor.location_id
            or not entity_visible_to_actor(state, actor.id, target.id)
        ):
            return "npc_reference_not_known", "An invitation must name another active, visible person in the current scene."
        return None
    assert isinstance(action, ResolveRelationshipInvitationIntent)
    invitation = next((item for item in state.relationship_invitations if item.invitation_id == action.invitation_id), None)
    if invitation is None or actor.id not in {invitation.proposer_id, invitation.invitee_id}:
        return "npc_reference_not_known", "An invitation response must refer to the actor's own invitation record."
    return None


def _validate_ballot_vote_reference(
    state: WorldState, actor: WorldEntity, action: IntentModel,
) -> tuple[str, str] | None:
    assert isinstance(action, CastLeadershipBallotVoteIntent)
    ballot = next((item for item in state.leadership_ballots if item.ballot_id == action.ballot_id), None)
    if ballot is None or actor.id not in ballot.eligible_voter_ids:
        return "npc_reference_not_known", "A vote must refer to a ballot in which the actor is an eligible voter."
    return None
