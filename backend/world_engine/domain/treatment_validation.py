"""Commit-bound validation for exact injury-treatment receipts."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from decimal import Decimal
from uuid import UUID, uuid5

from .errors import InvalidEvent
from .cultivation_breakthrough import (
    CultivationBreakthroughError,
    cultivation_breakthrough_effect_id,
    plan_cultivation_breakthrough,
    registered_cultivation_breakthrough_policy,
)
from .cultivation_breakthrough_events import (
    CultivationBreakthroughResolvedEvent,
    breakthrough_effect_events,
)
from .components import HealthComponent, LifeComponent, NeedsComponent, NeedValue, PopulationComponent
from .gameplay_events import (
    BackgroundNeedsAdvancedEvent,
    BackgroundRoutineAdvancedEvent,
    ComponentReplacedEvent,
    DamageAppliedEvent,
    HealthRestoredEvent,
    InjuryTreatedEvent,
    LifeEndedEvent,
    ResourceConsumedEvent,
)
from .models import DomainEvent, TimeAdvancedEvent, WorldState
from .ruleset_versions import CULTIVATION_DISABLED_MESSAGE, cultivation_disabled_by_world_rules


def validate_treatment_batch(
    events: Sequence[DomainEvent],
    *,
    world_id: UUID,
    branch_id: UUID,
    command_id: UUID,
    ruleset_version: str,
    actor_id: UUID | None = None,
    command_type: str | None = None,
    command_payload: Mapping[str, object] | None = None,
    state: WorldState | None = None,
) -> None:
    """Bind one treatment, its finite dose, and healing event to one command.

    Optional command and pre-state context allows the same check to be reused
    during historical event replay and backup validation when the archive does
    not retain the original request payload.
    """
    _validate_cultivation_breakthrough_batch(
        events,
        world_id=world_id,
        branch_id=branch_id,
        command_id=command_id,
        ruleset_version=ruleset_version,
        actor_id=actor_id,
        command_type=command_type,
        command_payload=command_payload,
        state=state,
    )

    treatments = [event for event in events if isinstance(event, InjuryTreatedEvent)]
    treatment_heals = [
        event for event in events
        if isinstance(event, HealthRestoredEvent) and event.method == "core.injury_treatment"
    ]
    treatment_supplies = [
        event for event in events
        if isinstance(event, ResourceConsumedEvent) and event.purpose == "injury_treatment"
    ]
    if len(treatments) > 1 or len(treatment_heals) > 1 or len(treatment_supplies) > 1:
        raise InvalidEvent("A command batch may treat at most one injury with one dose.")
    if not treatments:
        if treatment_heals or treatment_supplies:
            raise InvalidEvent("Injury healing and medicine consumption require a treatment event.")
        return

    treatment = treatments[0]
    if (
        treatment.world_id != world_id
        or treatment.branch_id != branch_id
        or treatment.command_id != command_id
        or treatment.ruleset_version != ruleset_version
        or actor_id is not None and treatment.healer_id != actor_id
    ):
        raise InvalidEvent("Injury treatment provenance does not match its command envelope.")
    if command_type is not None and command_type != "action.treat_injury":
        raise InvalidEvent("Injury treatment must be committed by a treat_injury action.")
    if command_payload is not None and (
        command_payload.get("kind") != "treat_injury"
        or str(command_payload.get("target_id")) != str(treatment.target_id)
        or str(command_payload.get("injury_id")) != str(treatment.injury_id)
        or command_payload.get("medicine_resource_id") != treatment.medicine_resource_id
    ):
        raise InvalidEvent("Injury treatment does not match the command's registered intent.")
    if state is not None:
        healer = state.entities.get(treatment.healer_id)
        if (
            healer is None
            or treatment.started_tick != state.current_tick
            or healer.location_id != treatment.location_id
        ):
            raise InvalidEvent("Injury treatment start context does not match the command's base state.")

    consumed_matches = [
        event for event in events
        if isinstance(event, ResourceConsumedEvent) and event.event_id == treatment.consumed_event_id
    ]
    if len(consumed_matches) != 1 or treatment_supplies != consumed_matches:
        raise InvalidEvent("Injury treatment must cite one matching medicine consumption event.")
    consumed = consumed_matches[0]
    if (
        consumed.consumer_id != treatment.healer_id
        or consumed.purpose != "injury_treatment"
        or len(consumed.flows) != 1
        or consumed.flows[0].resource_id != treatment.medicine_resource_id
        or consumed.flows[0].quantity != Decimal("1")
    ):
        raise InvalidEvent("Injury treatment receipt must consume exactly one unit of the selected medicine.")

    if treatment.healing_amount > 0:
        if len(treatment_heals) != 1:
            raise InvalidEvent("Injury treatment with a health deficit must include one matching restoration event.")
        healing = treatment_heals[0]
        if (
            healing.target_id != treatment.target_id
            or healing.amount != treatment.healing_amount
            or healing.expected_health_revision != treatment.expected_health_revision + 1
            or healing.source_event_ids != [treatment.consumed_event_id, treatment.event_id]
        ):
            raise InvalidEvent("Health restoration does not match the recorded injury treatment and dose.")
        if events.index(treatment) >= events.index(healing):
            raise InvalidEvent("Injury treatment must precede its health restoration event.")
    elif treatment_heals:
        raise InvalidEvent("Treatment at full health must not emit a health restoration event.")
    if events.index(consumed) >= events.index(treatment):
        raise InvalidEvent("Treatment supply must be consumed before the injury is marked treated.")

    clocks = [event for event in events if isinstance(event, TimeAdvancedEvent)]
    if len(clocks) != 1 or clocks[0].to_tick != treatment.resolved_tick:
        raise InvalidEvent("Injury treatment must commit its registered thirty-minute time advance.")


_BREAKTHROUGH_COMMAND = "action.attempt_cultivation_breakthrough"
_BREAKTHROUGH_KIND = "attempt_cultivation_breakthrough"


def _validate_cultivation_breakthrough_batch(
    events: Sequence[DomainEvent],
    *,
    world_id: UUID,
    branch_id: UUID,
    command_id: UUID,
    ruleset_version: str,
    actor_id: UUID | None,
    command_type: str | None,
    command_payload: Mapping[str, object] | None,
    state: WorldState | None,
) -> None:
    outcomes = [event for event in events if isinstance(event, CultivationBreakthroughResolvedEvent)]
    tagged_costs = [
        event for event in events
        if isinstance(event, ResourceConsumedEvent) and event.purpose == "cultivation_breakthrough"
    ]
    requested = command_type == _BREAKTHROUGH_COMMAND or (
        command_payload is not None and command_payload.get("kind") == _BREAKTHROUGH_KIND
    )
    has_footprint = bool(outcomes or tagged_costs)
    # Archive command payloads are optional, so a missing or mistyped typed
    # event is still recognizable from the IDs assigned before planning.
    attempt_id = cultivation_breakthrough_effect_id(command_id, ruleset_version, "attempt")
    deterministic_ids = {
        attempt_id,
        cultivation_breakthrough_effect_id(command_id, ruleset_version, "qi_consumption"),
        cultivation_breakthrough_effect_id(command_id, ruleset_version, "success"),
        cultivation_breakthrough_effect_id(command_id, ruleset_version, "failure_damage"),
        uuid5(attempt_id, f"{ruleset_version}:cultivation_breakthrough.inventory"),
    }
    has_footprint = has_footprint or any(event.event_id in deterministic_ids for event in events)
    if not has_footprint and not requested:
        return

    if len(outcomes) != 1:
        raise InvalidEvent("A cultivation breakthrough command must include exactly one typed outcome event.")
    if state is None or actor_id is None:
        raise InvalidEvent("Cultivation breakthrough replay validation requires its acting character's start state.")
    if cultivation_disabled_by_world_rules(ruleset_version, state.metadata):
        raise InvalidEvent(CULTIVATION_DISABLED_MESSAGE)
    outcome = outcomes[0]
    plan = outcome.plan
    if (
        (plan.world_id, plan.branch_id, plan.command_id, plan.ruleset_version, plan.actor_id)
        != (world_id, branch_id, command_id, ruleset_version, actor_id)
        or plan.started_tick != state.current_tick
        or outcome.event_id != plan.attempt_id
    ):
        raise InvalidEvent("Breakthrough outcome scope must exactly match its command and start state.")
    if command_type is not None and command_type != _BREAKTHROUGH_COMMAND:
        raise InvalidEvent("A cultivation breakthrough event must be committed by its registered timed action.")
    if command_payload is not None and (
        command_payload.get("kind") != _BREAKTHROUGH_KIND
        or command_payload.get("policy_id") != plan.policy_id
    ):
        raise InvalidEvent("Cultivation breakthrough must match its command's registered policy intent.")
    policy = registered_cultivation_breakthrough_policy(state, plan.policy_id)
    if policy is None:
        raise InvalidEvent("Cultivation breakthrough outcome names no policy registered for the start state.")
    try:
        expected_plan = plan_cultivation_breakthrough(
            state,
            actor_id=actor_id,
            policy=policy,
            world_id=world_id,
            branch_id=branch_id,
            command_id=command_id,
            ruleset_version=ruleset_version,
        )
        expected_effects = breakthrough_effect_events(state, expected_plan)
    except (CultivationBreakthroughError, ValueError) as exc:
        raise InvalidEvent(f"Cultivation breakthrough plan could not be recomputed: {exc}") from exc
    if plan != expected_plan:
        raise InvalidEvent("Stored breakthrough plan does not match the authoritative policy and start state.")

    clocks = [event for event in events if isinstance(event, TimeAdvancedEvent)]
    if len(clocks) != 1 or clocks[0].to_tick != plan.resolved_tick or not events or events[0] is not clocks[0]:
        raise InvalidEvent("A cultivation breakthrough must commit its exact registered time advance first.")
    prelude_end = 1
    if len(events) > 1 and isinstance(events[1], (BackgroundNeedsAdvancedEvent, BackgroundRoutineAdvancedEvent)):
        prelude_end = 2
    start = prelude_end
    effect_end = start + len(expected_effects)
    if effect_end > len(events) or list(events[start:effect_end]) != expected_effects:
        raise InvalidEvent("Breakthrough inventory, qi, outcome, and failure effects must match as one exact ordered batch.")

    # No other resource receipt or inventory/progression replacement is part of
    # this action. Needs and lifespan effects below are the only permitted tail.
    for event in [*events[:start], *events[effect_end:]]:
        if isinstance(event, ResourceConsumedEvent):
            raise InvalidEvent("A breakthrough command may consume only its exact registered qi flow.")
        if isinstance(event, ComponentReplacedEvent) and event.entity_id == actor_id and event.component_name in {
            "inventory", "progression",
        }:
            raise InvalidEvent("A breakthrough command may replace only the exact registered inventory and progression state.")

    tail = _expected_breakthrough_tail(
        state,
        plan,
        command_id=command_id,
        ruleset_version=ruleset_version,
    )
    actual_tail = list(events[effect_end:])
    if actual_tail != tail:
        raise InvalidEvent("Breakthrough needs, deprivation, and lifespan companions do not match their registered effects.")


def _expected_breakthrough_tail(
    state: WorldState,
    plan,
    *,
    command_id: UUID,
    ruleset_version: str,
) -> list[DomainEvent]:
    """Recompute the action's ordinary needs and exact-tick life companions."""

    from .components import LifeComponent
    from .gameplay_events import ComponentReplacedEvent, DamageAppliedEvent, LifeEndedEvent

    actor = state.entities[plan.actor_id]
    health = HealthComponent.model_validate(actor.components.get("health"))
    strain = plan.failure_damage if plan.outcome == "failed" else 0
    post_strain_health = max(0, health.current - strain)
    tail: list[DomainEvent] = []
    needs_after = None
    deprivation_damage = 0
    if post_strain_health > 0 and actor.components.get("needs") is not None:
        needs = NeedsComponent.model_validate(actor.components["needs"])
        needs_after, deprivation_damage = _advance_action_needs(
            needs,
            from_tick=plan.started_tick,
            to_tick=plan.resolved_tick,
            day_seconds=state.calendar.day_seconds,
        )
        if needs_after != needs:
            tail.append(ComponentReplacedEvent(
                event_id=uuid5(command_id, f"{ruleset_version}:needs.time"),
                entity_id=actor.id,
                component_name="needs",
                expected_revision=needs.revision,
                replacement=needs_after.model_dump(mode="json"),
            ))
    if post_strain_health > 0 and deprivation_damage > 0:
        expected_health_revision = health.revision + (1 if strain else 0)
        life = None
        try:
            life = LifeComponent.model_validate(actor.components.get("life"))
        except Exception:
            pass
        population_revision = None
        if life is not None and life.population_group_id is not None:
            cohort = state.entities[life.population_group_id]
            population_revision = PopulationComponent.model_validate(cohort.components["population"]).revision
        tail.append(DamageAppliedEvent(
            event_id=uuid5(command_id, f"{ruleset_version}:needs.deprivation_damage"),
            target_id=actor.id,
            damage=min(1_000_000, deprivation_damage),
            injury_type="core.injury.deprivation",
            description="Repeated critical hunger or thirst caused injury.",
            expected_health_revision=expected_health_revision,
            expected_population_revision=population_revision,
        ))

    actor_died_to_strain = post_strain_health == 0
    actor_died_to_deprivation = (
        not actor_died_to_strain and deprivation_damage >= post_strain_health and deprivation_damage > 0
    )
    lifespan_events: list[LifeEndedEvent] = []
    actor_lifespan_event: LifeEndedEvent | None = None
    for entity in sorted(state.entities.values(), key=lambda item: str(item.id)):
        if entity.kind not in {"character", "person"} or entity.status in {"dead", "retired"}:
            continue
        if entity.id == actor.id and (actor_died_to_strain or actor_died_to_deprivation):
            continue
        try:
            life = LifeComponent.model_validate(entity.components.get("life"))
        except Exception:
            continue
        if life.is_immortal or life.lifespan_ticks is None or life.birth_tick + life.lifespan_ticks != plan.resolved_tick:
            continue
        population_revision = None
        if life.population_group_id is not None:
            cohort = state.entities.get(life.population_group_id)
            if cohort is None:
                raise InvalidEvent("An exact-tick lifespan companion references a missing population cohort.")
            population_revision = PopulationComponent.model_validate(cohort.components["population"]).revision
        life_event = LifeEndedEvent(
            event_id=uuid5(
                command_id,
                f"{ruleset_version}:life.expired" if entity.id == actor.id
                else f"{ruleset_version}:life.expired:{entity.id}",
            ),
            entity_id=entity.id,
            expected_life_revision=life.revision,
            expected_population_revision=population_revision,
        )
        if entity.id == actor.id:
            actor_lifespan_event = life_event
        else:
            lifespan_events.append(life_event)
    if actor_lifespan_event is not None:
        tail.append(actor_lifespan_event)
    tail.extend(lifespan_events)
    return tail


def _advance_action_needs(
    needs: NeedsComponent,
    *,
    from_tick: int,
    to_tick: int,
    day_seconds: int,
) -> tuple[NeedsComponent, int]:
    """Mirror the rules engine's standard food, water, and rest time effect."""

    duration = to_tick - from_tick
    rates = {"food": 18, "water": 30, "rest": 24}
    updates: dict[str, NeedValue | None] = {}
    damage = 0
    for name, per_day in rates.items():
        value = getattr(needs, name)
        if value is None:
            continue
        numerator = per_day * duration + value.decay_remainder
        change = _trunc_div(numerator, day_seconds)
        remainder = numerator - change * day_seconds
        amount = max(0, min(100, value.value - change))
        marker = value.last_critical_damage_tick
        if name in {"food", "water"} and amount < value.critical_below:
            marker = marker if marker is not None else from_tick
            critical_seconds = to_tick - marker
            if critical_seconds >= day_seconds:
                days = critical_seconds // day_seconds
                damage += days
                marker += days * day_seconds
        else:
            marker = None
        updates[name] = value.model_copy(update={
            "value": amount,
            "decay_remainder": remainder,
            "last_satisfied_tick": value.last_satisfied_tick,
            "last_critical_damage_tick": marker,
        })
    changed = any(getattr(needs, name) != value for name, value in updates.items())
    if not changed:
        return needs, damage
    return needs.model_copy(update={**updates, "revision": needs.revision + 1}), damage


def _trunc_div(numerator: int, denominator: int) -> int:
    quotient = abs(numerator) // denominator
    return -quotient if numerator < 0 else quotient
