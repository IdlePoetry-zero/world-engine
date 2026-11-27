from __future__ import annotations

from uuid import UUID

from pydantic import BaseModel

from world_engine.application.autonomy_coverage import aggregate_autonomy_coverage


def _id(value: str) -> str:
    return str(UUID(int=int(value)))


def _entity(ident: str, kind: str, *, status: str = "active", name: str = "Sensitive Name", birth_tick: int = 0):
    components = {"life": {"birth_tick": birth_tick}} if kind in {"character", "person"} else {}
    return {"id": _id(ident), "kind": kind, "name": name, "status": status, "components": components}


def _row(event_type: str, tick: int, payload: dict, *, actor_id: str = "0", causes=(), committed=True):
    return {
        "event_type": event_type,
        "tick": tick,
        "actor_id": _id(actor_id),
        "payload_json": payload,
        "causation_json": [_id(item) for item in causes],
        "committed": committed,
    }


def _subject(report, ident: str):
    return next(row for row in report.author_detail["subjects"] if row["subject_id"] == _id(ident))


def test_coverage_uses_genesis_birth_exit_and_current_state_bounds_without_pii():
    genesis = {
        "current_tick": 0,
        "player_id": _id("3"),
        "entities": {
            _id("1"): _entity("1", "character", name="Ada"),
            _id("2"): _entity("2", "organization", name="Red Court"),
            _id("3"): _entity("3", "character", name="Player"),
        },
    }
    current_state = {
        "current_tick": 30,
        "player_id": _id("3"),
        "entities": {
            _id("1"): _entity("1", "character", status="dead", name="Ada"),
            _id("2"): _entity("2", "organization", status="closed", name="Red Court"),
            _id("3"): _entity("3", "character", name="Player"),
            _id("4"): _entity("4", "person", name="New Resident", birth_tick=3),
        },
    }
    events = iter((
        _row("core.entity_created.v1", 5, {"entity": _entity("4", "person", name="New Resident", birth_tick=3)}),
        _row("core.npc_plan_review_resolved.v1", 8, {
            "actor_id": _id("4"), "outcome": "defer", "reason": "provider_unavailable",
        }),
        _row("core.market_trade_executed.v1", 12, {"actor_id": _id("4")}),
        _row("core.life_ended.v1", 20, {"entity_id": _id("1")}),
        _row("core.entity_field_changed.v1", 25, {
            "entity_id": _id("2"), "field_name": "status", "value": "closed",
        }),
        _row("core.market_trade_executed.v1", 29, {"actor_id": _id("4")}, committed=False),
    ))

    report = aggregate_autonomy_coverage(genesis, events, current_state=current_state, period_ticks=10)

    ada = _subject(report, "1")
    resident = _subject(report, "4")
    organization = _subject(report, "2")
    assert ada["eligibility_intervals"] == [{
        "start_tick": 0, "end_tick_exclusive": 20, "entry_kind": "genesis", "exit_kind": "life_ended",
    }]
    assert ada["eligible_ticks_in_window"] == 20
    assert ada["no_action_reason"] == "unknown"
    assert resident["eligibility_intervals"] == [{
        "start_tick": 5, "end_tick_exclusive": 31, "entry_kind": "created", "exit_kind": "window_end",
    }]
    assert resident["review_count"] == 1
    assert resident["decision_count"] == 1
    assert resident["action_count"] == 1
    assert resident["effect_count"] == 1
    assert resident["no_action_reason"] == "not_applicable"
    assert organization["subject_class"] == "organization"
    assert organization["eligibility_intervals"][0]["end_tick_exclusive"] == 25
    assert organization["effect_count"] == 1
    assert _id("3") not in {row["subject_id"] for row in report.author_detail["subjects"]}

    portable = report.portable
    assert portable["actors"]["eligible_count"] == 2
    assert portable["actors"]["no_action_reason_unknown_count"] == 1
    assert portable["organizations"]["eligible_count"] == 1
    assert portable["ledger"]["ignored_uncommitted_count"] == 1
    assert "Sensitive Name" not in repr(portable)
    assert _id("1") not in repr(portable)
    assert "Ada" not in repr(report.author_detail)


def test_story_arrivals_and_ambient_causation_are_counted_from_committed_evidence():
    actor = _entity("11", "person", name="Arriving Person", birth_tick=-40)
    organization = _entity("12", "organization", name="Council")
    schedule_id = _id("13")
    ambient_event_id = _id("14")
    genesis = {
        "current_tick": 0,
        "entities": {_id("12"): organization},
        "story_arrivals": [{"actor": actor}],
        "scheduled_events": [{
            "id": schedule_id,
            "handler_id": "world.ambient_event",
            "payload": {"scope_kind": "organization", "scope_id": _id("12")},
        }],
    }
    current_state = {
        "current_tick": 20,
        "entities": {_id("11"): actor, _id("12"): organization},
    }
    events = [
        _row("core.story_milestone_resolved.v1", 10, {
            "arrival_world_id": _id("99"),
            "milestone": {"arrival_actor_ids": [_id("11")]},
        }),
        _row("core.ambient_event_resolved.v1", 12, {
            "event_id": ambient_event_id,
            "schedule_id": schedule_id,
            "outcome": "accepted",
            "proposal": {"initiator_id": _id("11"), "participant_ids": []},
        }),
        _row("core.movement_completed.v1", 15, {"actor_id": _id("11")}, causes=("14",)),
    ]

    report = aggregate_autonomy_coverage(genesis, events, current_state=current_state)

    arriving = _subject(report, "11")
    council = _subject(report, "12")
    assert arriving["eligibility_intervals"] == [{
        "start_tick": 10, "end_tick_exclusive": 21, "entry_kind": "arrival", "exit_kind": "window_end",
    }]
    assert arriving["ambient_followup_count"] == 1
    assert arriving["opportunity_count"] == 2  # accepted situation plus the committed movement action
    assert council["ambient_check_count"] == 1
    assert council["ambient_accepted_count"] == 1
    assert council["ambient_followup_count"] == 1


def test_unknown_is_explicit_when_no_action_or_reason_was_recorded():
    genesis = {"current_tick": 40, "entities": {_id("21"): _entity("21", "character")}}
    report = aggregate_autonomy_coverage(genesis, (), window_end_tick=49)
    actor = _subject(report, "21")
    assert actor["eligible_ticks_in_window"] == 10
    assert actor["no_action_reason"] == "unknown"
    assert actor["coverage_status"] == "no_recorded_event_reason_unknown"
    assert actor["opportunity_status"] == "no_recorded_opportunity_reason_unknown"
    assert report.portable["actors"]["eligible_without_recorded_action_count"] == 1
    assert report.portable["actors"]["no_action_reason_unknown_count"] == 1
    assert report.portable["actors"]["no_recorded_opportunity_reason_unknown_count"] == 1


def test_routine_manifest_and_organization_review_separate_decisions_actions_and_effects():
    actor = _entity("31", "character", name="Mira")
    organization = _entity("32", "organization", name="Guild")

    class EventEnvelope(BaseModel):
        event_type: str
        tick: int
        actor_id: UUID
        payload_json: dict[str, object]
        causation_json: list[str] = []

    routine = EventEnvelope(
        event_type="core.background_routine_advanced.v1",
        tick=10,
        actor_id=UUID(_id("0")),
        payload_json={"from_tick": 0, "to_tick": 10, "expected_agents": [{"entity_id": _id("31")}]},
    )
    strategy_review = _row("core.organization_strategy_review_resolved.v1", 15, {
        "review": {
            "organization_id": _id("32"),
            "reason": "action_executed",
            "authorized_actor_id": _id("31"),
            "transfer": {"quantity": "3"},
        },
    })
    current_state = {
        "current_tick": 20,
        "entities": {_id("31"): actor, _id("32"): organization},
    }

    report = aggregate_autonomy_coverage(
        {"current_tick": 0, "entities": {_id("31"): actor, _id("32"): organization}},
        (routine, strategy_review),
        current_state=current_state,
        period_ticks=10,
    )

    mira = _subject(report, "31")
    guild = _subject(report, "32")
    assert mira["routine_count"] == 1
    assert mira["action_count"] == 1
    assert mira["effect_count"] == 2  # routine needs plus the organization's committed transfer
    assert guild["review_count"] == 1
    assert guild["decision_count"] == 1
    assert guild["opportunity_count"] == 1
    assert guild["action_count"] == 1
    assert guild["effect_count"] == 1
    middle_period = report.portable["periods"][1]
    assert middle_period["actors"]["routine_count"] == 1
    assert middle_period["organizations"]["review_count"] == 1


def test_typed_exploration_outcomes_and_private_topic_inspection_reports_are_distinct():
    actor = _entity("41", "character", name="Reporter")
    listener = _entity("42", "person", name="Listener")
    organization = _entity("43", "organization", name="Council")
    private_actor = {"policy": "subject_ids", "subject_ids": [_id("41")]}
    private_pair = {"policy": "subject_ids", "subject_ids": sorted((_id("41"), _id("42")))}

    def record(
        record_id: str,
        *,
        owner_id: str = "41",
        holder_id: str | None = None,
        subject_id: str = "44",
        source_refs: list[str],
        kind: str,
        visibility: dict,
        outcome: str | None = None,
        received_tick: int,
    ):
        result = {
            "id": _id(record_id), "owner_id": _id(owner_id), "subject_id": _id(subject_id),
            "kind": kind, "claim": "Private text must not escape the author detail.",
            "received_tick": received_tick, "source_refs": [_id(item) for item in source_refs],
            "visibility": visibility,
        }
        if holder_id is not None:
            result["holder_id"] = _id(holder_id)
        if outcome is not None:
            result["topic_inspection"] = outcome
        return result

    def acquired(tick: int, receipt: dict, visibility: dict):
        event = _row("core.knowledge_acquired.v1", tick, {"record": receipt})
        event["visibility"] = visibility
        return event

    report_record = record(
        "47", holder_id="42", subject_id="41", source_refs=["45", "46"], kind="memory",
        visibility=private_pair, received_tick=5,
    )
    failed_report = record(
        "48", holder_id="42", subject_id="41", source_refs=["49", "50"], kind="memory",
        visibility=private_pair, received_tick=8,
    )
    events = [
        _row("core.npc_plan_revised.v1", 2, {
            "actor_id": _id("41"), "exploration_result": "discovered", "plan": {"actor_id": _id("41")},
        }),
        acquired(3, record(
            "45", subject_id="44", source_refs=["46"], kind="observation",
            visibility=private_actor, outcome="discovered", received_tick=3,
        ), private_actor),
        acquired(5, report_record, private_pair),
        _row("core.npc_plan_revised.v1", 6, {
            "actor_id": _id("41"), "exploration_result": "nothing_hidden_found", "plan": {"actor_id": _id("41")},
        }),
        acquired(7, record(
            "49", subject_id="44", source_refs=["50"], kind="observation",
            visibility=private_actor, outcome="search_failed", received_tick=7,
        ), private_actor),
        acquired(8, failed_report, private_pair),
    ]
    genesis = {
        "current_tick": 0,
        "entities": {
            _id("41"): actor, _id("42"): listener, _id("43"): organization,
        },
    }
    current_state = {"current_tick": 10, "entities": genesis["entities"]}

    report = aggregate_autonomy_coverage(genesis, events, current_state=current_state, period_ticks=10)

    reporter = _subject(report, "41")
    recipient = _subject(report, "42")
    council = _subject(report, "43")
    assert reporter["exploration_result_counts"] == {
        "discovered": 1, "nothing_hidden_found": 1, "search_failed": 0, "interrupted": 0,
    }
    assert reporter["topic_inspection_receipt_counts"] == {
        "discovered": 1, "nothing_hidden_found": 0, "search_failed": 1,
    }
    assert reporter["topic_inspection_report_count"] == 1
    assert reporter["decision_count"] == 2
    assert reporter["action_count"] == 3  # two plan results and one proven private report
    assert reporter["effect_count"] == 4  # receipts plus both pair-private memory events
    assert recipient["effect_count"] == 2
    assert recipient["topic_inspection_report_count"] == 0
    assert council["exploration_result_counts"] == {
        "discovered": 0, "nothing_hidden_found": 0, "search_failed": 0, "interrupted": 0,
    }
    assert council["topic_inspection_receipt_counts"] == {
        "discovered": 0, "nothing_hidden_found": 0, "search_failed": 0,
    }
    assert council["topic_inspection_report_count"] == 0
    assert report.portable["actors"]["topic_inspection_report_count"] == 1
    assert report.portable["organizations"]["topic_inspection_receipt_counts"]["discovered"] == 0
    assert _id("41") not in repr(report.portable)
    assert "Private text" not in repr(report.portable)
    assert "Private text" not in repr(report.author_detail)


def test_generic_exploration_result_does_not_invent_a_topic_inspection_receipt():
    actor = _entity("51", "person")
    genesis = {"current_tick": 0, "entities": {_id("51"): actor}}
    current_state = {"current_tick": 2, "entities": {_id("51"): actor}}
    report = aggregate_autonomy_coverage(
        genesis,
        (_row("core.npc_plan_revised.v1", 1, {
            "actor_id": _id("51"), "exploration_result": "nothing_hidden_found",
            "plan": {"actor_id": _id("51"), "steps": [{"action_id": "world.action.explore.v1", "arguments": {}}]},
        }),),
        current_state=current_state,
    )

    person = _subject(report, "51")
    assert person["exploration_result_counts"]["nothing_hidden_found"] == 1
    assert person["topic_inspection_receipt_counts"] == {
        "discovered": 0, "nothing_hidden_found": 0, "search_failed": 0,
    }
    assert person["topic_inspection_report_count"] == 0


def test_snapshot_topic_receipt_can_support_a_later_window_report_without_being_retallied():
    actor = _entity("61", "character")
    listener = _entity("62", "person")
    actor_visibility = {"policy": "subject_ids", "subject_ids": [_id("61")]}
    pair_visibility = {"policy": "subject_ids", "subject_ids": sorted((_id("61"), _id("62")))}
    inspection = {
        "id": _id("63"), "owner_id": _id("61"), "subject_id": _id("64"),
        "kind": "observation", "claim": "Private inspection text.", "received_tick": 2,
        "source_refs": [_id("65")], "topic_inspection": "discovered", "visibility": actor_visibility,
    }
    report_memory = {
        "id": _id("66"), "owner_id": _id("61"), "holder_id": _id("62"),
        "subject_id": _id("61"), "kind": "memory", "claim": "Private report text.",
        "received_tick": 5, "source_refs": [_id("63"), _id("65")], "visibility": pair_visibility,
    }
    report_event = _row("core.knowledge_acquired.v1", 5, {"record": report_memory})
    report_event["visibility"] = pair_visibility
    genesis = {
        "current_tick": 0,
        "entities": {_id("61"): actor, _id("62"): listener},
        "knowledge": [inspection],
    }
    current_state = {"current_tick": 9, "entities": genesis["entities"]}

    report = aggregate_autonomy_coverage(
        genesis, (report_event,), current_state=current_state,
        window_start_tick=4, window_end_tick=8,
    )

    reporter = _subject(report, "61")
    assert reporter["topic_inspection_receipt_counts"] == {
        "discovered": 0, "nothing_hidden_found": 0, "search_failed": 0,
    }
    assert reporter["topic_inspection_report_count"] == 1
    assert report.portable["actors"]["topic_inspection_receipt_counts"]["discovered"] == 0
    assert report.portable["actors"]["topic_inspection_report_count"] == 1
    assert _id("61") not in repr(report.portable)
    assert "Private inspection text" not in repr(report.portable)
    assert "Private report text" not in repr(report.author_detail)
