"""Pure, author-scoped aggregation of committed autonomous-world coverage.

The caller supplies an explicit genesis projection and committed events in
branch order.  This module never reads storage or models and never returns
entity names, event prose, locations, balances, or other world facts in its
portable aggregate.  The private author detail uses opaque entity UUIDs only.

Eligibility intervals are half-open internally.  ``window_end_tick`` is an
inclusive world tick; a subject active on that tick contributes one final
eligible tick.  Missing reasons are reported as ``"unknown"`` rather than
being inferred from silence.
"""

from __future__ import annotations

from collections import OrderedDict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
import json
from typing import Any, Literal
from uuid import UUID


_PEOPLE = frozenset({"character", "person"})
_ORGANIZATIONS = frozenset({"organization", "polity", "faction", "company", "government"})
_NPC_REVIEW = "core.npc_plan_review_resolved.v1"
_NPC_PLAN_TYPES = frozenset({"core.npc_plan_created.v1", "core.npc_plan_revised.v1"})
_ORG_REVIEW = "core.organization_strategy_review_resolved.v1"
_MEMBERSHIP_REVIEW = "core.organization_membership_reviewed.v1"
_ROUTINE = "core.background_routine_advanced.v1"
_BACKGROUND_NEEDS = "core.background_needs_advanced.v1"
_SOCIAL_CONTACT = "core.social_contact_settled.v1"
_AMBIENT = "core.ambient_event_resolved.v1"
_ENTITY_CREATED = "core.entity_created.v1"
_ENTITY_FIELD_CHANGED = "core.entity_field_changed.v1"
_LIFE_ENDED = "core.life_ended.v1"
_COHORT_PROMOTED = "core.cohort_member_promoted.v1"
_STORY_MILESTONE = "core.story_milestone_resolved.v1"
_PLAYER_CONTINUED = "core.player_character_continued.v1"
_KNOWLEDGE_ACQUIRED = "core.knowledge_acquired.v1"

_EXPLORATION_RESULTS = ("discovered", "nothing_hidden_found", "search_failed", "interrupted")
_TOPIC_INSPECTION_RESULTS = ("discovered", "nothing_hidden_found", "search_failed")

# These are committed outcomes or actions, not plans, prompts, or prose.
_DECISION_TYPES = frozenset({
    "core.npc_meeting_attendance_decision_recorded.v1",
    "core.waypoint_opportunity_decision_recorded.v1",
})
_ACTION_TYPES = frozenset({
    "core.npc_attendance_settled.v1",
    "core.work_shift_paid.v1",
    "core.travel_journey_changed.v1",
    "core.market_trade_executed.v1",
    "core.movement_completed.v1",
    "core.challenge_resolved.v1",
    "core.improvised_check_resolved.v1",
    "core.cultivation_breakthrough_resolved.v1",
    "core.combat_exchange_resolved.v1",
    "core.combat_retreat_resolved.v1",
    "core.investigation_case_opened.v1",
    "core.investigation_case_evidence_attached.v1",
    "core.investigation_case_closed.v1",
})
_EFFECT_TYPES = frozenset({
    "core.entity_field_changed.v1",
    "core.component_replaced.v1",
    "core.movement_completed.v1",
    "core.travel_journey_changed.v1",
    "core.market_trade_executed.v1",
    "core.work_shift_paid.v1",
    "core.life_ended.v1",
    "core.damage_applied.v1",
    "core.health_restored.v1",
    "core.injury_treated.v1",
    "core.relation_established.v1",
    "core.relation_dimension_changed.v1",
    "core.goal_created.v1",
    "core.goal_updated.v1",
    "core.organization_member_resigned.v1",
    "core.governance_succession.v1",
    "core.governance_leadership_handed_over.v1",
    "core.institutional_credit_changed.v1",
    "core.institutional_credit_extension_changed.v1",
    "core.institutional_treasury_mobilized.v1",
    _ROUTINE,
    _BACKGROUND_NEEDS,
    "core.npc_attendance_settled.v1",
    _KNOWLEDGE_ACQUIRED,
})
_REVIEW_OR_DECISION_TYPES = frozenset({
    _NPC_REVIEW,
    _ORG_REVIEW,
    _MEMBERSHIP_REVIEW,
    *_DECISION_TYPES,
    *_NPC_PLAN_TYPES,
})
_MAX_AMBIENT_CAUSATION_SOURCES = 4096
_MAX_TOPIC_INSPECTION_RECEIPTS = 4096
_MAX_PERIODS = 2048
_MAX_SUBJECT_PERIODS = 2_000_000


@dataclass(frozen=True, slots=True)
class AutonomyCoverageReport:
    """Separate aggregate-safe and author-only views of one coverage run."""

    portable: dict[str, Any]
    author_detail: dict[str, Any]


@dataclass(slots=True)
class _Counters:
    routine_count: int = 0
    review_count: int = 0
    decision_count: int = 0
    opportunity_count: int = 0
    action_count: int = 0
    effect_count: int = 0
    ambient_check_count: int = 0
    ambient_accepted_count: int = 0
    ambient_followup_count: int = 0
    other_event_count: int = 0
    exploration_result_counts: dict[str, int] = field(
        default_factory=lambda: dict.fromkeys(_EXPLORATION_RESULTS, 0),
    )
    topic_inspection_receipt_counts: dict[str, int] = field(
        default_factory=lambda: dict.fromkeys(_TOPIC_INSPECTION_RESULTS, 0),
    )
    topic_inspection_report_count: int = 0
    reason_codes: list[str] = field(default_factory=list)

    def add(self, other: _Counters) -> None:
        self.routine_count += other.routine_count
        self.review_count += other.review_count
        self.decision_count += other.decision_count
        self.opportunity_count += other.opportunity_count
        self.action_count += other.action_count
        self.effect_count += other.effect_count
        self.ambient_check_count += other.ambient_check_count
        self.ambient_accepted_count += other.ambient_accepted_count
        self.ambient_followup_count += other.ambient_followup_count
        self.other_event_count += other.other_event_count
        for outcome in _EXPLORATION_RESULTS:
            self.exploration_result_counts[outcome] += other.exploration_result_counts[outcome]
        for outcome in _TOPIC_INSPECTION_RESULTS:
            self.topic_inspection_receipt_counts[outcome] += other.topic_inspection_receipt_counts[outcome]
        self.topic_inspection_report_count += other.topic_inspection_report_count
        self.reason_codes.extend(other.reason_codes)

    def as_dict(self) -> dict[str, Any]:
        return {
            "routine_count": self.routine_count,
            "review_count": self.review_count,
            "decision_count": self.decision_count,
            "opportunity_count": self.opportunity_count,
            "action_count": self.action_count,
            "effect_count": self.effect_count,
            "ambient_check_count": self.ambient_check_count,
            "ambient_accepted_count": self.ambient_accepted_count,
            "ambient_followup_count": self.ambient_followup_count,
            "other_event_count": self.other_event_count,
            "exploration_result_counts": dict(self.exploration_result_counts),
            "topic_inspection_receipt_counts": dict(self.topic_inspection_receipt_counts),
            "topic_inspection_report_count": self.topic_inspection_report_count,
        }


@dataclass(slots=True)
class _Interval:
    start_tick: int
    end_tick_exclusive: int | None
    entry_kind: str
    exit_kind: str | None = None
    end_unknown: bool = False


@dataclass(slots=True)
class _Subject:
    subject_id: str
    subject_class: Literal["actor", "organization"]
    entity_kind: str
    active: bool
    controlled: bool
    excluded: bool
    open_start: int | None = None
    open_entry_kind: str = "unknown"
    intervals: list[_Interval] = field(default_factory=list)
    counters: _Counters = field(default_factory=_Counters)
    period_counters: dict[int, _Counters] = field(default_factory=dict)
    state_conflict: bool = False

    @property
    def eligible(self) -> bool:
        return self.active and not self.controlled and not self.excluded


@dataclass(slots=True)
class _Period:
    start_tick: int
    end_tick_exclusive: int
    actor_subjects: set[str] = field(default_factory=set)
    organization_subjects: set[str] = field(default_factory=set)
    actor_eligible_ticks: int = 0
    organization_eligible_ticks: int = 0
    actors: _Counters = field(default_factory=_Counters)
    organizations: _Counters = field(default_factory=_Counters)
    actors_without_action: int = 0
    actor_no_action_reason_unknown: int = 0
    actors_without_opportunity: int = 0
    actor_no_opportunity_reason_unknown: int = 0
    actors_with_opportunity_without_action: int = 0
    organizations_without_action: int = 0
    organization_no_action_reason_unknown: int = 0
    organizations_without_opportunity: int = 0
    organization_no_opportunity_reason_unknown: int = 0
    organizations_with_opportunity_without_action: int = 0


def aggregate_autonomy_coverage(
    genesis: object,
    committed_events: Iterable[object],
    *,
    current_state: object | None = None,
    window_start_tick: int | None = None,
    window_end_tick: int | None = None,
    period_ticks: int | None = None,
    excluded_actor_ids: Iterable[UUID | str] = (),
) -> AutonomyCoverageReport:
    """Aggregate autonomous coverage without retaining the committed ledger.

    ``genesis`` and ``current_state`` may be ``WorldState`` objects or their
    mapping forms.  ``committed_events`` is consumed once, in ascending branch
    sequence order.  Each entry may be a typed event (``model_dump``), an
    ``EventRecord``-like object, or an ORM/event mapping with ``event_type``,
    ``tick``, ``payload_json`` and optional ``causation_json``. The caller is
    responsible for author authorization and for selecting committed rows.

    A returned ``portable`` view contains only aggregate counts and tick
    ranges. ``author_detail`` contains opaque entity UUIDs and interval/count
    evidence but omits names, descriptions, locations, and event payload text.
    An absent action reason is always ``"unknown"``.
    """

    genesis_data = _as_mapping(genesis)
    if genesis_data is None:
        raise ValueError("genesis must be a WorldState or mapping")
    current_data = _as_mapping(current_state) if current_state is not None else None
    if current_state is not None and current_data is None:
        raise ValueError("current_state must be a WorldState or mapping")

    genesis_tick = _int(genesis_data.get("current_tick"), default=0)
    start_tick = _int(window_start_tick, default=genesis_tick)
    if window_end_tick is None:
        current_tick = _int(current_data.get("current_tick"), default=None) if current_data is not None else None
        end_inclusive = current_tick
    else:
        end_inclusive = _int(window_end_tick, default=None)
    if end_inclusive is not None and end_inclusive < start_tick:
        raise ValueError("window_end_tick must be greater than or equal to window_start_tick")
    if current_data is not None and end_inclusive is not None:
        snapshot_tick = _int(current_data.get("current_tick"), default=None)
        if end_inclusive > snapshot_tick:
            raise ValueError("window_end_tick cannot exceed the supplied current-state tick")
    if period_ticks is not None and (type(period_ticks) is not int or period_ticks <= 0):
        raise ValueError("period_ticks must be a positive integer")

    external_exclusions = {_uuid_text(item) for item in excluded_actor_ids}
    external_exclusions.discard("")
    subjects: dict[str, _Subject] = {}
    arrival_templates: dict[str, Mapping[str, Any]] = {}
    _seed_genesis(genesis_data, subjects, arrival_templates, external_exclusions)
    ambient_scopes = _ambient_schedule_scopes(genesis_data)
    if current_data is not None:
        ambient_scopes.update(_ambient_schedule_scopes(current_data))

    inferred_last_tick = genesis_tick
    event_count = 0
    ignored_uncommitted_count = 0
    ambient_source_overflow_count = 0
    ambient_sources: OrderedDict[str, tuple[tuple[str, ...], tuple[str, ...]]] = OrderedDict()
    topic_inspection_receipts: OrderedDict[str, tuple[str, str, str, int]] = OrderedDict()
    topic_inspection_source_overflow_count = _seed_topic_inspection_receipts(
        genesis_data, topic_inspection_receipts,
    )
    supplied_end_exclusive = end_inclusive + 1 if end_inclusive is not None else None

    for raw_event in committed_events:
        row, payload, event_type, event_tick, causation_ids, is_committed = _event_parts(raw_event)
        if not is_committed:
            ignored_uncommitted_count += 1
            continue
        event_count += 1
        inferred_last_tick = max(inferred_last_tick, event_tick)

        # Lifecycle events before the requested window are still necessary to
        # know who was eligible at window start. Later events are irrelevant to
        # an explicitly bounded read.
        if supplied_end_exclusive is not None and event_tick >= supplied_end_exclusive:
            continue

        if event_type == _ENTITY_CREATED:
            entity = _mapping_value(payload.get("entity"))
            if entity is not None:
                _register_new_entity(
                    entity, event_tick, subjects, external_exclusions, entry_kind="created",
                )
        elif event_type == _COHORT_PROMOTED:
            entity = _mapping_value(payload.get("character"))
            if entity is not None:
                _register_new_entity(
                    entity, event_tick, subjects, external_exclusions, entry_kind="promotion",
                )
        elif event_type == _STORY_MILESTONE:
            milestone = _mapping_value(payload.get("milestone")) or {}
            if payload.get("arrival_world_id") is not None:
                for actor_id in _strings(milestone.get("arrival_actor_ids")):
                    template = arrival_templates.get(actor_id)
                    if template is not None:
                        _register_new_entity(
                            template, event_tick, subjects, external_exclusions, entry_kind="arrival",
                        )

        if event_type == _ENTITY_FIELD_CHANGED and payload.get("field_name") == "status":
            subject_id = _uuid_text(payload.get("entity_id"))
            subject = subjects.get(subject_id)
            if subject is not None:
                if (
                    subject.eligible
                    and payload.get("value") != "active"
                    and _in_window(event_tick, start_tick, supplied_end_exclusive)
                ):
                    _record(subject, "effect_count", event_tick, start_tick, period_ticks)
                _set_active(subject, payload.get("value") == "active", event_tick, "status_change")
        elif event_type == _LIFE_ENDED:
            subject_id = _uuid_text(payload.get("entity_id"))
            subject = subjects.get(subject_id)
            if subject is not None:
                # Attribute the explicit terminal effect to the actor's final
                # eligible boundary before closing the interval.
                if _in_window(event_tick, start_tick, supplied_end_exclusive):
                    _record(subject, "effect_count", event_tick, start_tick, period_ticks)
                _set_active(subject, False, event_tick, "life_ended")
        elif event_type == _PLAYER_CONTINUED:
            old_id = _uuid_text(payload.get("previous_player_id"))
            new_id = _uuid_text(payload.get("new_player_id"))
            old_subject = subjects.get(old_id)
            new_subject = subjects.get(new_id)
            if old_subject is not None:
                _set_controlled(old_subject, False, event_tick)
            if new_subject is not None:
                _set_controlled(new_subject, True, event_tick)

        inspection_receipt = None
        inspection_report = None
        if event_type == _KNOWLEDGE_ACQUIRED:
            record = _mapping_value(payload.get("record")) or {}
            event_visibility = row.get("visibility_json", row.get("visibility", payload.get("visibility")))
            inspection_receipt = _topic_inspection_receipt(record, event_visibility, event_tick)
            inspection_report = _topic_inspection_report(
                record, event_visibility, event_tick, topic_inspection_receipts,
            )
            if inspection_receipt is not None:
                receipt_id, owner_id, topic_id, outcome, received_tick = inspection_receipt
                if receipt_id in topic_inspection_receipts:
                    del topic_inspection_receipts[receipt_id]
                elif len(topic_inspection_receipts) >= _MAX_TOPIC_INSPECTION_RECEIPTS:
                    topic_inspection_receipts.popitem(last=False)
                    topic_inspection_source_overflow_count += 1
                topic_inspection_receipts[receipt_id] = (owner_id, topic_id, outcome, received_tick)

        in_window = _in_window(event_tick, start_tick, supplied_end_exclusive)
        if not in_window:
            continue

        if event_type in {_ROUTINE, _BACKGROUND_NEEDS}:
            for agent in _sequence(payload.get("expected_agents")):
                agent_data = _mapping_value(agent) or {}
                subject = subjects.get(_uuid_text(agent_data.get("entity_id")))
                if subject is not None and subject.eligible:
                    _record(subject, "routine_count", event_tick, start_tick, period_ticks)
        elif event_type == _SOCIAL_CONTACT:
            for choice in _sequence(payload.get("choices")):
                choice_data = _mapping_value(choice) or {}
                opportunity = _mapping_value(choice_data.get("opportunity")) or {}
                slot_tick = _int(opportunity.get("slot_tick"), default=event_tick)
                for participant_id in _strings(opportunity.get("participant_ids")):
                    subject = subjects.get(participant_id)
                    if subject is not None and subject.eligible and _in_window(
                        slot_tick, start_tick, supplied_end_exclusive,
                    ):
                        _record(subject, "routine_count", slot_tick, start_tick, period_ticks)
                        _record(subject, "opportunity_count", slot_tick, start_tick, period_ticks)
        elif event_type == _NPC_REVIEW:
            actor_id = _uuid_text(payload.get("actor_id"))
            subject = subjects.get(actor_id)
            if subject is not None and subject.eligible:
                _record(subject, "review_count", event_tick, start_tick, period_ticks)
                _record(subject, "decision_count", event_tick, start_tick, period_ticks)
                _record(subject, "opportunity_count", event_tick, start_tick, period_ticks)
                if payload.get("outcome") not in {"ready", None}:
                    _record_reason(subject, payload.get("reason"), event_tick, start_tick, period_ticks)
        elif event_type in _NPC_PLAN_TYPES:
            actor_id = _uuid_text(payload.get("actor_id"))
            subject = subjects.get(actor_id)
            if subject is not None and subject.eligible:
                _record(subject, "decision_count", event_tick, start_tick, period_ticks)
                _record(subject, "opportunity_count", event_tick, start_tick, period_ticks)
                exploration_result = payload.get("exploration_result")
                if event_type == "core.npc_plan_revised.v1" and exploration_result in _EXPLORATION_RESULTS:
                    _record_exploration_result(
                        subject, exploration_result, event_tick, start_tick, period_ticks,
                    )
        elif event_type == _ORG_REVIEW:
            review = _mapping_value(payload.get("review")) or {}
            organization = subjects.get(_uuid_text(review.get("organization_id")))
            if organization is not None and organization.eligible:
                _record(organization, "review_count", event_tick, start_tick, period_ticks)
                _record(organization, "decision_count", event_tick, start_tick, period_ticks)
                _record(organization, "opportunity_count", event_tick, start_tick, period_ticks)
                reason = review.get("reason")
                if reason != "action_executed":
                    _record_reason(organization, reason, event_tick, start_tick, period_ticks)
                if reason == "action_executed":
                    _record(organization, "action_count", event_tick, start_tick, period_ticks)
                    if review.get("transfer") is not None:
                        _record(organization, "effect_count", event_tick, start_tick, period_ticks)
                    actor = subjects.get(_uuid_text(review.get("authorized_actor_id")))
                    if actor is not None and actor.eligible:
                        _record(actor, "opportunity_count", event_tick, start_tick, period_ticks)
                        _record(actor, "action_count", event_tick, start_tick, period_ticks)
                        if review.get("transfer") is not None:
                            _record(actor, "effect_count", event_tick, start_tick, period_ticks)
        elif event_type == _MEMBERSHIP_REVIEW:
            review = _mapping_value(payload.get("review")) or {}
            for organization_report in _sequence(review.get("organizations")):
                report = _mapping_value(organization_report) or {}
                organization = subjects.get(_uuid_text(report.get("organization_id")))
                if organization is not None and organization.eligible:
                    _record(organization, "review_count", event_tick, start_tick, period_ticks)
                    _record(organization, "decision_count", event_tick, start_tick, period_ticks)
                    _record(organization, "opportunity_count", event_tick, start_tick, period_ticks)
        elif event_type in _DECISION_TYPES:
            actor_id = _uuid_text(payload.get("actor_id"))
            subject = subjects.get(actor_id)
            if subject is not None and subject.eligible:
                _record(subject, "decision_count", event_tick, start_tick, period_ticks)
                _record(subject, "opportunity_count", event_tick, start_tick, period_ticks)
                _record(subject, "opportunity_count", event_tick, start_tick, period_ticks)

        if inspection_receipt is not None:
            _, owner_id, _, outcome, _ = inspection_receipt
            owner = subjects.get(owner_id)
            if owner is not None and owner.eligible:
                _record_topic_inspection_receipt(owner, outcome, event_tick, start_tick, period_ticks)
        if inspection_report is not None:
            speaker_id, _recipient_id = inspection_report
            speaker = subjects.get(speaker_id)
            if speaker is not None and speaker.eligible:
                _record(speaker, "topic_inspection_report_count", event_tick, start_tick, period_ticks)
                _record(speaker, "opportunity_count", event_tick, start_tick, period_ticks)
                _record(speaker, "action_count", event_tick, start_tick, period_ticks)

        if event_type == _AMBIENT:
            _record_ambient_check(
                payload, event_tick, start_tick, period_ticks, subjects, ambient_scopes,
            )
            if payload.get("outcome") == "accepted":
                proposal = _mapping_value(payload.get("proposal")) or {}
                affected = tuple(dict.fromkeys((
                    _uuid_text(proposal.get("initiator_id")),
                    *_strings(proposal.get("participant_ids")),
                )))
                affected = tuple(item for item in affected if item)
                scope_id, scope_kind = ambient_scopes.get(_uuid_text(payload.get("schedule_id")), ("", ""))
                scoped_orgs = (scope_id,) if scope_kind == "organization" and scope_id else ()
                source_id = _uuid_text(payload.get("event_id"))
                if source_id:
                    if len(ambient_sources) >= _MAX_AMBIENT_CAUSATION_SOURCES:
                        ambient_sources.popitem(last=False)
                        ambient_source_overflow_count += 1
                    ambient_sources[source_id] = (affected, scoped_orgs)

        _record_generic_event(
            event_type, row, payload, event_tick, start_tick, period_ticks,
            causation_ids, subjects,
        )

        # A later committed event may cite an accepted ambient event as its
        # cause. Attribute only the causal receipt, not its prose or proposal.
        caused_sources = [ambient_sources.get(cause_id) for cause_id in causation_ids]
        caused_sources = [source for source in caused_sources if source is not None]
        if caused_sources:
            direct_actors = _action_actor_ids(event_type, row, payload)
            for actor_id in direct_actors:
                subject = subjects.get(actor_id)
                if subject is not None and subject.eligible:
                    if any(actor_id in source[0] for source in caused_sources):
                        _record(subject, "ambient_followup_count", event_tick, start_tick, period_ticks)
            for source in caused_sources:
                for organization_id in source[1]:
                    organization = subjects.get(organization_id)
                    if organization is not None and organization.eligible:
                        _record(organization, "ambient_followup_count", event_tick, start_tick, period_ticks)

    if end_inclusive is None:
        end_inclusive = max(start_tick, inferred_last_tick)
    end_exclusive = end_inclusive + 1
    if period_ticks is None:
        period_ticks = max(1, end_exclusive - start_tick)
    if (end_exclusive - start_tick + period_ticks - 1) // period_ticks > _MAX_PERIODS:
        raise ValueError(f"coverage windows may contain at most {_MAX_PERIODS} periods")

    periods = _new_periods(start_tick, end_exclusive, period_ticks)
    current_entities = _entity_index(current_data) if current_data is not None else None
    if current_data is not None:
        current_player_id = _uuid_text(current_data.get("player_id"))
    else:
        current_player_id = ""

    eligible_ticks: dict[str, int] = {"actor": 0, "organization": 0}
    eligible_subjects: dict[str, int] = {"actor": 0, "organization": 0}
    incomplete_subjects: dict[str, int] = {"actor": 0, "organization": 0}
    unknown_no_action: dict[str, int] = {"actor": 0, "organization": 0}
    active_no_action: dict[str, int] = {"actor": 0, "organization": 0}
    active_no_opportunity: dict[str, int] = {"actor": 0, "organization": 0}
    active_opportunity_without_action: dict[str, int] = {"actor": 0, "organization": 0}
    total_counters = {"actor": _Counters(), "organization": _Counters()}
    detail_subjects: list[dict[str, Any]] = []
    subject_period_work = 0

    for subject in subjects.values():
        snapshot_entity = current_entities.get(subject.subject_id) if current_entities is not None else None
        if snapshot_entity is not None and subject.active != (_field(snapshot_entity, "status") == "active"):
            subject.state_conflict = True

        # The current snapshot bounds an open interval. If the snapshot says a
        # subject is inactive and no exit event explains when, preserve the
        # uncertainty instead of assigning a guessed duration.
        if subject.eligible and subject.open_start is not None:
            snapshot_entity = current_entities.get(subject.subject_id) if current_entities is not None else None
            snapshot_active = _field(snapshot_entity, "status") == "active" if snapshot_entity is not None else None
            snapshot_controlled = subject.subject_class == "actor" and subject.subject_id == current_player_id
            if snapshot_active is False or snapshot_controlled:
                subject.intervals.append(_Interval(
                    start_tick=subject.open_start,
                    end_tick_exclusive=None,
                    entry_kind=subject.open_entry_kind,
                    exit_kind="unknown",
                    end_unknown=True,
                ))
                subject.open_start = None
                subject.state_conflict = True
            else:
                subject.intervals.append(_Interval(
                    start_tick=subject.open_start,
                    end_tick_exclusive=end_exclusive,
                    entry_kind=subject.open_entry_kind,
                    exit_kind="window_end",
                ))
                subject.open_start = None

        category = subject.subject_class
        counters = _Counters()
        counters.add(subject.counters)
        total_counters[category].add(counters)
        intervals_json: list[dict[str, Any]] = []
        subject_ticks = 0
        has_unknown_interval = False
        eligible_period_indexes: set[int] = set()
        for interval in subject.intervals:
            if interval.end_unknown or interval.end_tick_exclusive is None:
                has_unknown_interval = True
                intervals_json.append({
                    "start_tick": interval.start_tick,
                    "end_tick_exclusive": None,
                    "entry_kind": interval.entry_kind,
                    "exit_kind": "unknown",
                })
                continue
            clipped_start = max(start_tick, interval.start_tick)
            clipped_end = min(end_exclusive, interval.end_tick_exclusive)
            if clipped_end <= clipped_start:
                continue
            intervals_json.append({
                "start_tick": clipped_start,
                "end_tick_exclusive": clipped_end,
                "entry_kind": interval.entry_kind,
                "exit_kind": interval.exit_kind,
            })
            subject_ticks += clipped_end - clipped_start
            _add_interval_to_periods(
                interval=interval,
                start_tick=start_tick,
                end_exclusive=end_exclusive,
                period_ticks=period_ticks,
                periods=periods,
                subject_id=subject.subject_id,
                subject_class=category,
            )
            first_period = (clipped_start - start_tick) // period_ticks
            last_period = (clipped_end - 1 - start_tick) // period_ticks
            eligible_period_indexes.update(range(first_period, last_period + 1))
            subject_period_work += last_period - first_period + 1

        if subject_period_work > _MAX_SUBJECT_PERIODS:
            raise ValueError("coverage detail exceeds the bounded subject-period aggregation limit")
        has_eligibility = subject_ticks > 0
        if has_eligibility:
            eligible_subjects[category] += 1
            eligible_ticks[category] += subject_ticks
        elif has_unknown_interval:
            incomplete_subjects[category] += 1
        if has_eligibility and subject.counters.action_count == 0:
            active_no_action[category] += 1
            if not subject.counters.reason_codes:
                unknown_no_action[category] += 1
            if subject.counters.opportunity_count > 0:
                active_opportunity_without_action[category] += 1
        if has_eligibility and subject.counters.opportunity_count == 0:
            active_no_opportunity[category] += 1

        for index, period_counters in subject.period_counters.items():
            if index < len(periods):
                target = periods[index].actors if category == "actor" else periods[index].organizations
                target.add(period_counters)
        for index in eligible_period_indexes:
            period_counters = subject.period_counters.get(index, _Counters())
            if period_counters.action_count == 0:
                if category == "actor":
                    periods[index].actors_without_action += 1
                    if not period_counters.reason_codes:
                        periods[index].actor_no_action_reason_unknown += 1
                    if period_counters.opportunity_count > 0:
                        periods[index].actors_with_opportunity_without_action += 1
                else:
                    periods[index].organizations_without_action += 1
                    if not period_counters.reason_codes:
                        periods[index].organization_no_action_reason_unknown += 1
                    if period_counters.opportunity_count > 0:
                        periods[index].organizations_with_opportunity_without_action += 1
            if period_counters.opportunity_count == 0:
                if category == "actor":
                    periods[index].actors_without_opportunity += 1
                    periods[index].actor_no_opportunity_reason_unknown += 1
                else:
                    periods[index].organizations_without_opportunity += 1
                    periods[index].organization_no_opportunity_reason_unknown += 1

        latest_reason = _latest_reason(subject.counters.reason_codes)
        no_action_reason = "not_applicable" if subject.counters.action_count else latest_reason or "unknown"
        if subject.counters.action_count:
            coverage_status = "action_recorded"
        elif subject.counters.opportunity_count:
            coverage_status = "recorded_opportunity_without_action"
        elif any((subject.counters.routine_count, subject.counters.review_count,
                  subject.counters.decision_count, subject.counters.effect_count,
                  subject.counters.other_event_count)):
            coverage_status = "activity_without_recorded_opportunity"
        else:
            coverage_status = "no_recorded_event_reason_unknown"
        if not (has_eligibility or has_unknown_interval or subject.counters.action_count
                or subject.counters.review_count or subject.counters.decision_count
                or subject.counters.routine_count or subject.counters.opportunity_count
                or subject.counters.effect_count or subject.counters.other_event_count):
            continue
        detail_subjects.append({
            "subject_id": subject.subject_id,
            "subject_class": subject.subject_class,
            "entity_kind": subject.entity_kind,
            "eligibility_intervals": intervals_json,
            "eligible_ticks_in_window": subject_ticks if not has_unknown_interval else None,
            "eligibility_incomplete": has_unknown_interval or subject.state_conflict,
            **subject.counters.as_dict(),
            "recorded_non_action_reasons": list(dict.fromkeys(subject.counters.reason_codes)),
            "no_action_reason": no_action_reason,
            "coverage_status": coverage_status,
            "opportunity_status": (
                "recorded" if subject.counters.opportunity_count
                else "no_recorded_opportunity_reason_unknown"
            ),
            "period_activity": _subject_period_detail(subject, start_tick, end_exclusive, period_ticks),
        })

    subject_periods = sum(len(subject.period_counters) for subject in subjects.values())
    if subject_periods > _MAX_SUBJECT_PERIODS:
        raise ValueError("coverage detail exceeds the bounded subject-period aggregation limit")

    period_rows: list[dict[str, Any]] = []
    for index, period in enumerate(periods):
        period_rows.append({
            "start_tick": period.start_tick,
            "end_tick_exclusive": period.end_tick_exclusive,
            "actors": {
                "eligible_count": len(period.actor_subjects),
                "eligible_ticks": period.actor_eligible_ticks,
                "eligible_without_recorded_action_count": period.actors_without_action,
                "no_action_reason_unknown_count": period.actor_no_action_reason_unknown,
                "eligible_without_recorded_opportunity_count": period.actors_without_opportunity,
                "no_recorded_opportunity_reason_unknown_count": period.actor_no_opportunity_reason_unknown,
                "opportunity_without_recorded_action_count": period.actors_with_opportunity_without_action,
                **period.actors.as_dict(),
            },
            "organizations": {
                "eligible_count": len(period.organization_subjects),
                "eligible_ticks": period.organization_eligible_ticks,
                "eligible_without_recorded_action_count": period.organizations_without_action,
                "no_action_reason_unknown_count": period.organization_no_action_reason_unknown,
                "eligible_without_recorded_opportunity_count": period.organizations_without_opportunity,
                "no_recorded_opportunity_reason_unknown_count": period.organization_no_opportunity_reason_unknown,
                "opportunity_without_recorded_action_count": period.organizations_with_opportunity_without_action,
                **period.organizations.as_dict(),
            },
        })

    aggregate = {
        "schema_version": 1,
        "view": "portable_aggregate",
        "contains_entity_identifiers": False,
        "contains_names_or_event_prose": False,
        "window": {"start_tick": start_tick, "end_tick_exclusive": end_exclusive},
        "period_ticks": period_ticks,
        "actors": _category_summary(
            eligible_subjects["actor"], eligible_ticks["actor"], total_counters["actor"],
            active_no_action["actor"], unknown_no_action["actor"], incomplete_subjects["actor"],
            active_no_opportunity["actor"], active_opportunity_without_action["actor"],
        ),
        "organizations": _category_summary(
            eligible_subjects["organization"], eligible_ticks["organization"], total_counters["organization"],
            active_no_action["organization"], unknown_no_action["organization"], incomplete_subjects["organization"],
            active_no_opportunity["organization"], active_opportunity_without_action["organization"],
        ),
        "periods": period_rows,
        "ledger": {
            "committed_event_count": event_count,
            "ignored_uncommitted_count": ignored_uncommitted_count,
            "ambient_causation_sources_dropped": ambient_source_overflow_count,
            "topic_inspection_sources_dropped": topic_inspection_source_overflow_count,
        },
    }
    detail = {
        "schema_version": 1,
        "view": "author_detail",
        "window": {"start_tick": start_tick, "end_tick_exclusive": end_exclusive},
        "subjects": sorted(detail_subjects, key=lambda item: (item["subject_class"], item["subject_id"])),
    }
    return AutonomyCoverageReport(portable=aggregate, author_detail=detail)


def _seed_genesis(
    genesis: Mapping[str, Any],
    subjects: dict[str, _Subject],
    arrival_templates: dict[str, Mapping[str, Any]],
    external_exclusions: set[str],
) -> None:
    genesis_tick = _int(genesis.get("current_tick"), default=0)
    player_id = _uuid_text(genesis.get("player_id"))
    for key, raw_entity in _entity_index(genesis).items():
        entity = _mapping_value(raw_entity) or {}
        subject = _subject_for_entity(entity, external_exclusions, player_id)
        if subject is None:
            continue
        subject.active = _field(entity, "status") == "active"
        if subject.active and subject.eligible:
            subject.open_start = max(genesis_tick, _birth_tick(entity, genesis_tick))
            subject.open_entry_kind = "genesis"
        subjects[key] = subject

    for raw_arrival in _sequence(genesis.get("story_arrivals")):
        arrival = _mapping_value(raw_arrival) or {}
        entity = _mapping_value(arrival.get("actor"))
        if entity is None:
            continue
        entity_id = _uuid_text(entity.get("id"))
        if entity_id and entity_id not in subjects and _kind_class(_field(entity, "kind")) == "actor":
            arrival_templates[entity_id] = entity


def _subject_for_entity(
    entity: Mapping[str, Any], external_exclusions: set[str], player_id: str = "",
) -> _Subject | None:
    kind = str(_field(entity, "kind") or "")
    subject_class = _kind_class(kind)
    subject_id = _uuid_text(_field(entity, "id"))
    if subject_class is None or not subject_id:
        return None
    controlled = subject_class == "actor" and subject_id == player_id
    excluded = subject_class == "actor" and subject_id in external_exclusions
    return _Subject(
        subject_id=subject_id,
        subject_class=subject_class,
        entity_kind=kind,
        active=_field(entity, "status") == "active",
        controlled=controlled,
        excluded=excluded,
    )


def _register_new_entity(
    entity: Mapping[str, Any],
    tick: int,
    subjects: dict[str, _Subject],
    external_exclusions: set[str],
    *,
    entry_kind: str,
) -> None:
    subject = _subject_for_entity(entity, external_exclusions)
    if subject is None or subject.subject_id in subjects:
        return
    # New world entities are inactive before their committed entry event.
    subject.active = False
    subject.controlled = False
    subjects[subject.subject_id] = subject
    if _field(entity, "status") == "active":
        _set_active(subject, True, max(tick, _birth_tick(entity, tick)), entry_kind)


def _set_active(subject: _Subject, active: bool, tick: int, entry_or_exit: str) -> None:
    if subject.active == active:
        return
    if active:
        subject.active = True
        if subject.eligible:
            subject.open_start = tick
            subject.open_entry_kind = entry_or_exit
        return
    if subject.eligible and subject.open_start is not None:
        subject.intervals.append(_Interval(
            start_tick=subject.open_start,
            end_tick_exclusive=tick,
            entry_kind=subject.open_entry_kind,
            exit_kind=entry_or_exit,
        ))
    subject.open_start = None
    subject.active = False


def _set_controlled(subject: _Subject, controlled: bool, tick: int) -> None:
    if subject.controlled == controlled:
        return
    if subject.eligible and subject.open_start is not None:
        subject.intervals.append(_Interval(
            start_tick=subject.open_start,
            end_tick_exclusive=tick,
            entry_kind=subject.open_entry_kind,
            exit_kind="player_control" if controlled else "autonomous_control",
        ))
    subject.open_start = None
    subject.controlled = controlled
    if subject.eligible:
        subject.open_start = tick
        subject.open_entry_kind = "autonomous_control"


def _record_generic_event(
    event_type: str,
    row: Mapping[str, Any],
    payload: Mapping[str, Any],
    tick: int,
    start_tick: int,
    period_ticks: int | None,
    causation_ids: tuple[str, ...],
    subjects: dict[str, _Subject],
) -> None:
    # Organization reviews are classified above from their validated review
    # record, which contains the exact organization and authorized actor.
    if event_type == _ORG_REVIEW:
        return
    action_ids = _action_actor_ids(event_type, row, payload)
    is_action = _is_action(event_type, payload)
    related_ids = _related_subject_ids(event_type, payload, subjects)
    for subject_id in action_ids:
        subject = subjects.get(subject_id)
        if subject is None or not subject.eligible:
            continue
        if is_action:
            _record(subject, "opportunity_count", tick, start_tick, period_ticks)
            _record(subject, "action_count", tick, start_tick, period_ticks)
    if event_type == _SOCIAL_CONTACT:
        effect_ids: set[str] = set()
        for raw_effect in _sequence(payload.get("relationship_effects")):
            effect = _mapping_value(raw_effect) or {}
            effect_ids.update(
                candidate for candidate in (
                    _uuid_text(effect.get("from_id")), _uuid_text(effect.get("to_id")),
                ) if candidate in subjects
            )
        is_effect = bool(effect_ids) or bool(causation_ids)
    else:
        effect_ids = related_ids | set(action_ids)
        is_effect = event_type in _EFFECT_TYPES or bool(causation_ids)
    if is_effect:
        for subject_id in effect_ids:
            subject = subjects.get(subject_id)
            if subject is not None and subject.eligible:
                _record(subject, "effect_count", tick, start_tick, period_ticks)
    if not is_action and not is_effect and event_type not in _REVIEW_OR_DECISION_TYPES | {
        _ROUTINE, _SOCIAL_CONTACT, _AMBIENT, _ENTITY_CREATED, _STORY_MILESTONE,
        _COHORT_PROMOTED, _LIFE_ENDED, _PLAYER_CONTINUED,
    }:
        for subject_id in related_ids | set(action_ids):
            subject = subjects.get(subject_id)
            if subject is not None and subject.eligible:
                _record(subject, "other_event_count", tick, start_tick, period_ticks)


def _is_action(event_type: str, payload: Mapping[str, Any]) -> bool:
    if event_type == _ORG_REVIEW:
        review = _mapping_value(payload.get("review")) or {}
        return review.get("reason") == "action_executed"
    if event_type == "core.travel_journey_changed.v1":
        journey = _mapping_value(payload.get("journey")) or {}
        return journey.get("operation", payload.get("operation")) == "depart"
    return event_type in _ACTION_TYPES


def _action_actor_ids(event_type: str, row: Mapping[str, Any], payload: Mapping[str, Any]) -> set[str]:
    if event_type in {_ROUTINE, _BACKGROUND_NEEDS}:
        return set()
    if event_type == _SOCIAL_CONTACT:
        return set()
    if event_type == "core.npc_attendance_settled.v1":
        attendance = _mapping_value(payload.get("attendance")) or {}
        attendee = _uuid_text(attendance.get("actor_id"))
        return {attendee} if attendee else set()
    actor_id = _uuid_text(payload.get("actor_id"))
    if not actor_id:
        actor_id = _uuid_text(row.get("actor_id"))
    result = {actor_id} if actor_id else set()
    if event_type == _ORG_REVIEW:
        review = _mapping_value(payload.get("review")) or {}
        authorized = _uuid_text(review.get("authorized_actor_id"))
        if authorized:
            result.add(authorized)
    return result


def _related_subject_ids(
    event_type: str, payload: Mapping[str, Any], subjects: Mapping[str, _Subject],
) -> set[str]:
    result: set[str] = set()
    keys = (
        "actor_id", "entity_id", "subject_id", "target_id", "person_id", "worker_id",
        "member_id", "from_id", "to_id", "previous_player_id", "new_player_id",
        "victim_id", "owner_actor_id", "counterparty_id",
    )
    for key in keys:
        candidate = _uuid_text(payload.get(key))
        if candidate in subjects:
            result.add(candidate)
    for nested_key in ("attendance", "plan", "record", "review"):
        nested = _mapping_value(payload.get(nested_key))
        if nested is None:
            continue
        for key in (
            "actor_id", "entity_id", "organization_id", "authorized_actor_id", "owner_id", "holder_id",
        ):
            candidate = _uuid_text(nested.get(key))
            if candidate in subjects:
                result.add(candidate)
    if event_type == _SOCIAL_CONTACT:
        for choice in _sequence(payload.get("choices")):
            choice_data = _mapping_value(choice) or {}
            opportunity = _mapping_value(choice_data.get("opportunity")) or {}
            result.update(item for item in _strings(opportunity.get("participant_ids")) if item in subjects)
    if event_type in {_ROUTINE, _BACKGROUND_NEEDS}:
        for agent in _sequence(payload.get("expected_agents")):
            agent_data = _mapping_value(agent) or {}
            candidate = _uuid_text(agent_data.get("entity_id"))
            if candidate in subjects:
                result.add(candidate)
    return result


def _record_ambient_check(
    payload: Mapping[str, Any],
    tick: int,
    start_tick: int,
    period_ticks: int | None,
    subjects: dict[str, _Subject],
    ambient_scopes: Mapping[str, tuple[str, str]],
) -> None:
    schedule_id = _uuid_text(payload.get("schedule_id"))
    scope_id, scope_kind = ambient_scopes.get(schedule_id, ("", ""))
    if scope_kind == "organization":
        organization = subjects.get(scope_id)
        if organization is not None and organization.eligible:
            _record(organization, "ambient_check_count", tick, start_tick, period_ticks)
            _record(organization, "opportunity_count", tick, start_tick, period_ticks)
            if payload.get("outcome") == "accepted":
                _record(organization, "ambient_accepted_count", tick, start_tick, period_ticks)
    if payload.get("outcome") == "accepted":
        proposal = _mapping_value(payload.get("proposal")) or {}
        for actor_id in set(_strings(proposal.get("participant_ids"))) | {
            _uuid_text(proposal.get("initiator_id")),
        }:
            actor = subjects.get(actor_id)
            if actor is not None and actor.eligible:
                _record(actor, "opportunity_count", tick, start_tick, period_ticks)


def _record(subject: _Subject, counter: str, tick: int, start_tick: int, period_ticks: int | None) -> None:
    if not hasattr(subject.counters, counter):
        return
    setattr(subject.counters, counter, getattr(subject.counters, counter) + 1)
    index = 0 if period_ticks is None else max(0, (tick - start_tick) // period_ticks)
    period = subject.period_counters.setdefault(index, _Counters())
    setattr(period, counter, getattr(period, counter) + 1)


def _record_outcome(
    subject: _Subject,
    counter: str,
    outcome: str,
    allowed: Sequence[str],
    tick: int,
    start_tick: int,
    period_ticks: int | None,
) -> None:
    if outcome not in allowed:
        return
    getattr(subject.counters, counter)[outcome] += 1
    index = 0 if period_ticks is None else max(0, (tick - start_tick) // period_ticks)
    getattr(subject.period_counters.setdefault(index, _Counters()), counter)[outcome] += 1


def _record_exploration_result(
    subject: _Subject, outcome: str, tick: int, start_tick: int, period_ticks: int | None,
) -> None:
    _record_outcome(
        subject, "exploration_result_counts", outcome, _EXPLORATION_RESULTS,
        tick, start_tick, period_ticks,
    )
    # A typed plan execution result is a committed attempt, including an
    # explicit failed or interrupted outcome.
    _record(subject, "action_count", tick, start_tick, period_ticks)


def _record_topic_inspection_receipt(
    subject: _Subject, outcome: str, tick: int, start_tick: int, period_ticks: int | None,
) -> None:
    _record_outcome(
        subject, "topic_inspection_receipt_counts", outcome, _TOPIC_INSPECTION_RESULTS,
        tick, start_tick, period_ticks,
    )


def _topic_inspection_receipt(
    record: Mapping[str, Any], event_visibility: object, event_tick: int,
) -> tuple[str, str, str, str, int] | None:
    receipt = _topic_inspection_record(record, expected_event_tick=event_tick)
    if receipt is None:
        return None
    receipt_id, owner_id, topic_id, outcome, received_tick = receipt
    if not _private_visibility(event_visibility, (owner_id,)):
        return None
    return receipt_id, owner_id, topic_id, outcome, received_tick


def _topic_inspection_record(
    record: Mapping[str, Any], *, expected_event_tick: int | None = None,
) -> tuple[str, str, str, str, int] | None:
    outcome = record.get("topic_inspection")
    owner_id = _uuid_text(record.get("owner_id"))
    subject_id = _uuid_text(record.get("subject_id"))
    record_id = _uuid_text(record.get("id"))
    raw_source_refs = _sequence(record.get("source_refs"))
    source_refs = [_uuid_text(value) for value in raw_source_refs]
    received_tick = record.get("received_tick")
    if (
        outcome not in _TOPIC_INSPECTION_RESULTS
        or record.get("kind") != "observation"
        or not owner_id or not subject_id or not record_id
        or record.get("holder_id") is not None
        or len(source_refs) != 1 or not source_refs[0]
        or type(received_tick) is not int
        or (expected_event_tick is not None and received_tick != expected_event_tick)
        or not _private_visibility(record.get("visibility"), (owner_id,))
    ):
        return None
    return record_id, owner_id, source_refs[0], outcome, received_tick


def _seed_topic_inspection_receipts(
    state: Mapping[str, Any], receipts: OrderedDict[str, tuple[str, str, str, int]],
) -> int:
    overflow_count = 0
    for raw_record in _sequence(state.get("knowledge")):
        record = _mapping_value(raw_record)
        if record is None:
            continue
        receipt = _topic_inspection_record(record)
        if receipt is None:
            continue
        receipt_id, owner_id, topic_id, outcome, received_tick = receipt
        if receipt_id in receipts:
            del receipts[receipt_id]
        elif len(receipts) >= _MAX_TOPIC_INSPECTION_RECEIPTS:
            receipts.popitem(last=False)
            overflow_count += 1
        receipts[receipt_id] = (owner_id, topic_id, outcome, received_tick)
    return overflow_count


def _topic_inspection_report(
    record: Mapping[str, Any],
    event_visibility: object,
    event_tick: int,
    receipts: Mapping[str, tuple[str, str, str, int]],
) -> tuple[str, str] | None:
    owner_id = _uuid_text(record.get("owner_id"))
    holder_id = _uuid_text(record.get("holder_id"))
    raw_source_refs = _sequence(record.get("source_refs"))
    source_refs = [_uuid_text(value) for value in raw_source_refs]
    received_tick = record.get("received_tick")
    if (
        record.get("kind") != "memory"
        or not owner_id or not holder_id or owner_id == holder_id
        or _uuid_text(record.get("subject_id")) != owner_id
        or len(source_refs) != 2 or not all(source_refs)
        or type(received_tick) is not int or received_tick != event_tick
        or not _private_visibility(record.get("visibility"), (owner_id, holder_id))
        or not _private_visibility(event_visibility, (owner_id, holder_id))
    ):
        return None
    receipt = receipts.get(source_refs[0])
    if receipt is None:
        return None
    receipt_owner_id, topic_id, outcome, receipt_tick = receipt
    if (
        receipt_owner_id != owner_id
        or topic_id != source_refs[1]
        or outcome not in {"discovered", "nothing_hidden_found"}
        or receipt_tick > event_tick
    ):
        return None
    return owner_id, holder_id


def _private_visibility(value: object, participant_ids: Sequence[str]) -> bool:
    visibility = _mapping_value(value) or {}
    expected = set(participant_ids)
    actual_values = _strings(visibility.get("subject_ids"))
    actual = set(actual_values)
    return (
        visibility.get("policy") == "subject_ids"
        and len(actual_values) == len(participant_ids)
        and actual == expected
    )


def _record_reason(subject: _Subject, reason: object, tick: int, start_tick: int, period_ticks: int | None) -> None:
    code = _reason_code(reason)
    if code:
        subject.counters.reason_codes.append(code)
        index = 0 if period_ticks is None else max(0, (tick - start_tick) // period_ticks)
        subject.period_counters.setdefault(index, _Counters()).reason_codes.append(code)


def _new_periods(start_tick: int, end_exclusive: int, period_ticks: int) -> list[_Period]:
    if end_exclusive <= start_tick:
        return [_Period(start_tick=start_tick, end_tick_exclusive=start_tick)]
    count = (end_exclusive - start_tick + period_ticks - 1) // period_ticks
    return [
        _Period(
            start_tick=start_tick + index * period_ticks,
            end_tick_exclusive=min(end_exclusive, start_tick + (index + 1) * period_ticks),
        )
        for index in range(count)
    ]


def _add_interval_to_periods(
    *,
    interval: _Interval,
    start_tick: int,
    end_exclusive: int,
    period_ticks: int,
    periods: list[_Period],
    subject_id: str,
    subject_class: str,
) -> None:
    if interval.end_tick_exclusive is None:
        return
    clipped_start = max(start_tick, interval.start_tick)
    clipped_end = min(end_exclusive, interval.end_tick_exclusive)
    if clipped_end <= clipped_start:
        return
    first = (clipped_start - start_tick) // period_ticks
    last = (clipped_end - 1 - start_tick) // period_ticks
    for index in range(first, last + 1):
        period = periods[index]
        overlap = max(0, min(clipped_end, period.end_tick_exclusive) - max(clipped_start, period.start_tick))
        if subject_class == "actor":
            period.actor_subjects.add(subject_id)
            period.actor_eligible_ticks += overlap
        else:
            period.organization_subjects.add(subject_id)
            period.organization_eligible_ticks += overlap


def _subject_period_detail(subject: _Subject, start_tick: int, end_exclusive: int, period_ticks: int) -> list[dict[str, Any]]:
    result = []
    for index, counters in sorted(subject.period_counters.items()):
        period_start = start_tick + index * period_ticks
        if period_start >= end_exclusive:
            continue
        result.append({
            "start_tick": period_start,
            "end_tick_exclusive": min(end_exclusive, period_start + period_ticks),
            **counters.as_dict(),
        })
    return result


def _category_summary(
    eligible_count: int,
    eligible_ticks: int,
    counters: _Counters,
    no_action_count: int,
    unknown_no_action_count: int,
    incomplete_count: int,
    no_opportunity_count: int,
    opportunity_without_action_count: int,
) -> dict[str, Any]:
    return {
        "eligible_count": eligible_count,
        "eligible_ticks": eligible_ticks,
        **counters.as_dict(),
        "eligible_without_recorded_action_count": no_action_count,
        "no_action_reason_unknown_count": unknown_no_action_count,
        "eligible_without_recorded_opportunity_count": no_opportunity_count,
        "no_recorded_opportunity_reason_unknown_count": no_opportunity_count,
        "opportunity_without_recorded_action_count": opportunity_without_action_count,
        "eligibility_incomplete_count": incomplete_count,
    }


def _event_parts(raw_event: object) -> tuple[Mapping[str, Any], Mapping[str, Any], str, int, tuple[str, ...], bool]:
    row = _as_mapping(raw_event)
    if row is None:
        raise ValueError("each committed event must be a typed event, record object, or mapping")
    event_type = str(row.get("event_type") or row.get("type") or "")
    payload_raw = row.get("payload_json", row.get("payload"))
    payload = _mapping_value(payload_raw)
    if payload is None:
        # Typed domain events expose their payload fields directly.
        payload = row
    tick_value = row.get("tick")
    if tick_value is None:
        tick_value = next((payload.get(key) for key in (
            "at_tick", "checked_tick", "resolved_tick", "effective_tick", "reviewed_tick",
            "current_tick", "to_tick", "arrived_tick", "completed_tick", "departure_tick",
            "departed_tick", "started_tick",
        ) if payload.get(key) is not None), None)
    if tick_value is None:
        for nested_key in ("milestone", "review", "plan", "attendance", "record", "journey", "attempt"):
            nested = _mapping_value(payload.get(nested_key))
            if nested is None:
                continue
            tick_value = next((nested.get(key) for key in (
                "resolved_tick", "reviewed_tick", "updated_tick", "at_tick", "effective_tick",
                "completed_tick", "checked_tick", "arrived_tick", "departed_tick", "started_tick",
            ) if nested.get(key) is not None), None)
            if tick_value is not None:
                break
    if tick_value is None:
        raise ValueError("committed event coverage requires its exact ledger tick")
    tick = _int(tick_value, default=0)
    causes = row.get("causation_json", row.get("causation_ids", payload.get("causation_ids", ())))
    causation_ids = tuple(item for item in (_uuid_text(value) for value in _sequence(causes)) if item)
    committed = row.get("is_committed", row.get("committed", True))
    return row, payload, event_type, tick, causation_ids, committed is not False


def _ambient_schedule_scopes(state: Mapping[str, Any]) -> dict[str, tuple[str, str]]:
    scopes: dict[str, tuple[str, str]] = {}
    for raw_schedule in _sequence(state.get("scheduled_events")):
        schedule = _mapping_value(raw_schedule) or {}
        if _field(schedule, "handler_id") != "world.ambient_event":
            continue
        payload = _mapping_value(schedule.get("payload")) or {}
        schedule_id = _uuid_text(_field(schedule, "id"))
        scope_id = _uuid_text(payload.get("scope_id"))
        scope_kind = str(payload.get("scope_kind") or "")
        if schedule_id and scope_id and scope_kind in {"region", "organization"}:
            scopes[schedule_id] = (scope_id, scope_kind)
    return scopes


def _entity_index(state: Mapping[str, Any] | None) -> dict[str, Any]:
    if state is None:
        return {}
    raw_entities = state.get("entities")
    if not isinstance(raw_entities, Mapping):
        return {}
    result = {}
    for key, entity in raw_entities.items():
        entity_data = _mapping_value(entity)
        entity_id = _uuid_text(entity_data.get("id")) if entity_data is not None else ""
        key_id = _uuid_text(key)
        identity = entity_id or key_id
        if identity:
            result[identity] = entity_data if entity_data is not None else entity
    return result


def _birth_tick(entity: Mapping[str, Any], default: int) -> int:
    components = _mapping_value(entity.get("components")) or {}
    life = _mapping_value(components.get("life")) or {}
    return _int(life.get("birth_tick"), default=default)


def _kind_class(kind: object) -> Literal["actor", "organization"] | None:
    if kind in _PEOPLE:
        return "actor"
    if kind in _ORGANIZATIONS:
        return "organization"
    return None


def _in_window(tick: int, start_tick: int, end_exclusive: int | None) -> bool:
    return tick >= start_tick and (end_exclusive is None or tick < end_exclusive)


def _latest_reason(reasons: Sequence[str]) -> str | None:
    return reasons[-1] if reasons else None


def _reason_code(value: object) -> str | None:
    if not isinstance(value, str) or not value or len(value) > 80:
        return None
    if not all(char.isascii() and (char.isalnum() or char in "_.-") for char in value):
        return None
    return value


def _uuid_text(value: object) -> str:
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, str):
        try:
            return str(UUID(value))
        except (ValueError, AttributeError):
            return ""
    return ""


def _strings(value: object) -> list[str]:
    return [text for text in (_uuid_text(item) for item in _sequence(value)) if text]


def _sequence(value: object) -> Sequence[Any]:
    if isinstance(value, (list, tuple)):
        return value
    return ()


def _int(value: object, *, default: int | None) -> int:
    if type(value) is int:
        return value
    if default is None:
        raise ValueError("coverage tick values must be integers")
    return default


def _as_mapping(value: object) -> Mapping[str, Any] | None:
    if isinstance(value, Mapping):
        return value
    dump = getattr(value, "model_dump", None)
    if callable(dump):
        try:
            dumped = dump(mode="python")
        except TypeError:
            dumped = dump()
        return dumped if isinstance(dumped, Mapping) else None
    # SQLAlchemy EventRow and similar read-only records are intentionally
    # accepted without importing their storage module.
    if value is None:
        return None
    fields = (
        "event_type", "type", "tick", "payload_json", "payload", "actor_id",
        "causation_json", "causation_ids", "is_committed", "committed",
        "sequence", "commit_boundary", "visibility_json", "visibility",
    )
    extracted = {name: getattr(value, name) for name in fields if hasattr(value, name)}
    return extracted if extracted else None


def _mapping_value(value: object) -> Mapping[str, Any] | None:
    if isinstance(value, Mapping):
        return value
    if isinstance(value, str):
        try:
            decoded = json.loads(value)
        except (json.JSONDecodeError, TypeError):
            return None
        return decoded if isinstance(decoded, Mapping) else None
    return _as_mapping(value)


def _field(value: object, key: str) -> Any:
    mapping = _mapping_value(value)
    if mapping is not None:
        return mapping.get(key)
    return getattr(value, key, None)
