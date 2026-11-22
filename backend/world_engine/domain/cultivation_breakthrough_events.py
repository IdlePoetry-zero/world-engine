"""Command-bound event and effect construction for cultivation breakthroughs."""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal
from uuid import UUID, uuid5

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .cultivation_breakthrough import CultivationBreakthroughPlan
from .json_types import JsonObject

if TYPE_CHECKING:
    from .models import DomainEvent, WorldState


class _BreakthroughEventModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        str_strip_whitespace=True,
        validate_assignment=True,
        allow_inf_nan=False,
    )


class CultivationBreakthroughResolvedEvent(_BreakthroughEventModel):
    """Private audit of one deterministic, timed breakthrough outcome."""

    type: Literal["core.cultivation_breakthrough_resolved.v1"] = "core.cultivation_breakthrough_resolved.v1"
    event_id: UUID
    plan: CultivationBreakthroughPlan
    visibility: JsonObject

    @model_validator(mode="after")
    def attempt_id_and_visibility_are_bound(self) -> CultivationBreakthroughResolvedEvent:
        if self.event_id != self.plan.attempt_id:
            raise ValueError("breakthrough outcome event ID must equal its registered attempt ID")
        if self.visibility != breakthrough_visibility(self.plan.actor_id):
            raise ValueError("breakthrough outcome details must be visible only to the acting character")
        return self


def breakthrough_visibility(actor_id: UUID) -> JsonObject:
    """Return the canonical participant-only visibility for an attempt."""

    if not isinstance(actor_id, UUID):
        raise ValueError("breakthrough visibility requires an actor UUID")
    return {"policy": "subject_ids", "subject_ids": [str(actor_id)]}


def breakthrough_inventory_event_id(plan: CultivationBreakthroughPlan) -> UUID:
    """Derive the stable identity for the exact FIFO inventory replacement."""

    return uuid5(plan.attempt_id, f"{plan.ruleset_version}:cultivation_breakthrough.inventory")


def breakthrough_effect_events(state: WorldState, plan: CultivationBreakthroughPlan) -> list[DomainEvent]:
    """Build the complete ordered event suffix for a planned breakthrough.

    The caller owns the clock advance. The exact FIFO inventory and qi ledger
    effects always precede the private outcome audit. A failed attempt appends
    one strain injury, with the linked population revision captured only when
    that injury is lethal.
    """

    from .components import HealthComponent, LifeComponent, PopulationComponent
    from .gameplay_events import (
        ComponentReplacedEvent,
        ConsumedResourceFlow,
        DamageAppliedEvent,
        ResourceConsumedEvent,
    )

    actor = state.entities.get(plan.actor_id)
    if actor is None:
        raise ValueError("breakthrough effect construction requires its planned actor in the start state")
    visibility = breakthrough_visibility(plan.actor_id)
    effects: list[DomainEvent] = [
        ComponentReplacedEvent(
            event_id=breakthrough_inventory_event_id(plan),
            entity_id=plan.actor_id,
            component_name="inventory",
            expected_revision=plan.expected_inventory_revision,
            replacement=plan.resulting_inventory.model_dump(mode="json"),
            visibility=visibility,
        ),
        ResourceConsumedEvent(
            event_id=plan.qi_consumption_event_id,
            consumer_id=plan.actor_id,
            purpose="cultivation_breakthrough",
            flows=[ConsumedResourceFlow(**flow.model_dump()) for flow in plan.resource_flows],
            visibility=visibility,
        ),
        CultivationBreakthroughResolvedEvent(
            event_id=plan.attempt_id,
            plan=plan,
            visibility=visibility,
        ),
    ]
    if plan.outcome == "failed":
        if plan.failure_damage_event_id is None or plan.failure_damage is None or plan.failure_injury_type is None:
            raise ValueError("failed breakthrough plan has no registered strain consequence")
        try:
            health = HealthComponent.model_validate(actor.components.get("health"))
        except Exception as exc:
            raise ValueError("breakthrough failure target has no valid health component") from exc
        expected_population_revision = None
        if plan.failure_damage >= health.current:
            try:
                life = LifeComponent.model_validate(actor.components.get("life"))
            except Exception:
                life = None
            if life is not None and life.population_group_id is not None:
                cohort = state.entities.get(life.population_group_id)
                if cohort is None:
                    raise ValueError("lethal breakthrough strain references a missing population cohort")
                try:
                    expected_population_revision = PopulationComponent.model_validate(
                        cohort.components.get("population")
                    ).revision
                except Exception as exc:
                    raise ValueError("lethal breakthrough strain references an invalid population cohort") from exc
        effects.append(DamageAppliedEvent(
            event_id=plan.failure_damage_event_id,
            target_id=plan.actor_id,
            damage=plan.failure_damage,
            injury_type=plan.failure_injury_type,
            description="Strain sustained during a failed cultivation breakthrough.",
            expected_health_revision=plan.expected_health_revision,
            expected_population_revision=expected_population_revision,
            source_event_ids=[],
            visibility=visibility,
        ))
    return effects
