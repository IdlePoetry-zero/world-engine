from __future__ import annotations

from decimal import Decimal
from uuid import uuid4

import pytest

from world_engine.application.backups import BackupApplication
from world_engine.application.simulation_jobs import SimulationJobs
from world_engine.domain.components import (
    InventoryComponent,
    PlaceComponent,
    RenewableHarvestPolicy,
    RenewableHarvestRecipient,
    ScheduledEventRecord,
)
from world_engine.domain.errors import InvalidEvent
from world_engine.domain.gameplay_events import RenewableResourceHarvestedEvent
from world_engine.domain.models import AccessScope, JobStatus, NewWorld, ResourceAccount, TimeAdvancedEvent, WorldEntity, WorldState
from world_engine.domain.reducers import apply_event, ledger_postings
from world_engine.rules import RuleContext, RuleEngine
from world_engine.simulation.contracts import WorldAdvancePlan
from world_engine.storage.backup_import import WorldBackupImporter
from world_engine.storage.database import Database
from world_engine.storage.repository import WorldRepository


PERIOD_TICKS = 30 * 86_400


def _state():
    region_id, person_id, source_id, holder_id, schedule_id = (uuid4() for _ in range(5))
    policy = RenewableHarvestPolicy(
        policy_id="monthly.food",
        resource_id="core.food",
        source_account_id=source_id,
        period_ticks=PERIOD_TICKS,
        per_period=Decimal("2"),
        maximum_total=Decimal("5"),
        recipients=[
            RenewableHarvestRecipient(
                entity_id=person_id,
                account_id=holder_id,
                per_period=Decimal("2"),
            )
        ],
    )
    region = WorldEntity(
        id=region_id,
        kind="place",
        name="North region",
        components={"place": PlaceComponent(place_type="region", renewable_harvests=[policy])},
    )
    person = WorldEntity(
        id=person_id,
        kind="character",
        name="Nara",
        location_id=region_id,
        components={"inventory": InventoryComponent()},
    )
    schedule = ScheduledEventRecord(
        id=schedule_id,
        due_tick=PERIOD_TICKS,
        phase="environment",
        handler_id="environmental.harvest",
        handler_version="1.0.0",
        repeat_ticks=PERIOD_TICKS,
        max_runs=3,
    )
    state = WorldState(
        current_tick=PERIOD_TICKS,
        player_id=person_id,
        entities={region_id: region, person_id: person},
        resources={
            source_id: ResourceAccount(
                resource_id="core.food",
                balance="0",
                owner_id=region_id,
                account_type="environmental_source",
                credit_limit="0",
            ),
            holder_id: ResourceAccount(
                resource_id="core.food",
                balance="0",
                owner_id=person_id,
                account_type="holder",
            ),
        },
        scheduled_events=[schedule],
    )
    context = RuleContext(
        world_id=uuid4(),
        branch_id=uuid4(),
        command_id=uuid4(),
        actor_id=person_id,
        ruleset_version="1.0.0",
    )
    return state, context, region_id, person_id, source_id, holder_id


def _due_harvest(state: WorldState, context: RuleContext):
    resolution = RuleEngine().resolve_scheduled(state, state.scheduled_events[0], context)
    assert resolution.outcome.status == "succeeded"
    return resolution


def _apply_resolution(state: WorldState, resolution):
    next_state = state
    for event in resolution.events:
        next_state = apply_event(next_state, event)
    return next_state


def test_scheduled_harvest_distributes_finite_rations_to_owned_lots():
    state, context, region_id, person_id, source_id, holder_id = _state()

    first = _due_harvest(state, context)
    harvest = next(event for event in first.events if isinstance(event, RenewableResourceHarvestedEvent))
    postings = ledger_postings(harvest)
    assert [(posting.account_id, posting.delta) for posting in postings] == [
        (source_id, Decimal("-2")),
        (holder_id, Decimal("2")),
    ]
    state = _apply_resolution(state, first)
    assert state.resources[source_id].balance == Decimal("-2")
    assert state.resources[source_id].credit_limit == Decimal("2")
    assert state.resources[holder_id].balance == Decimal("2")
    lot = state.entities[person_id].components["inventory"]["lots"][0]
    assert lot["quantity"] == "2"
    assert lot["origin_event_id"] == str(harvest.event_id)
    policy = state.entities[region_id].components["place"]["renewable_harvests"][0]
    assert policy["issued_total"] == "2"
    assert policy["last_issued_period"] == 1
    assert state.scheduled_events[0].due_tick == 2 * PERIOD_TICKS

    state = apply_event(state, TimeAdvancedEvent(to_tick=2 * PERIOD_TICKS))
    second = _due_harvest(state, context)
    state = _apply_resolution(state, second)
    assert state.resources[source_id].balance == Decimal("-4")
    assert state.resources[holder_id].balance == Decimal("4")
    assert len(state.entities[person_id].components["inventory"]["lots"]) == 2

    state = apply_event(state, TimeAdvancedEvent(to_tick=3 * PERIOD_TICKS))
    third = _due_harvest(state, context)
    state = _apply_resolution(state, third)
    assert state.resources[source_id].balance == Decimal("-5")
    assert state.resources[source_id].credit_limit == Decimal("5")
    assert state.resources[holder_id].balance == Decimal("5")
    assert [lot["quantity"] for lot in state.entities[person_id].components["inventory"]["lots"]] == ["2", "2", "1"]
    assert state.scheduled_events[0].status == "completed"


def test_harvest_reducer_rejects_stale_recipient_balance_and_schedule_mismatch():
    state, context, _region_id, _person_id, _source_id, _holder_id = _state()
    resolution = _due_harvest(state, context)
    harvest = next(event for event in resolution.events if isinstance(event, RenewableResourceHarvestedEvent))
    item = harvest.items[0]
    recipient = item.recipients[0]
    bad_recipient = recipient.model_copy(update={"expected_balance": Decimal("1")})
    bad_item = item.model_copy(update={"recipients": [bad_recipient]})
    bad_event = harvest.model_copy(update={"items": [bad_item]})
    with pytest.raises(InvalidEvent, match="stale or has the wrong owner/resource"):
        apply_event(state, bad_event)

    bad_schedule = state.scheduled_events[0].model_copy(update={"repeat_ticks": PERIOD_TICKS + 1})
    bad_state = WorldState.model_validate({
        **state.model_dump(mode="json"),
        "scheduled_events": [bad_schedule.model_dump(mode="json")],
    })
    rejection = RuleEngine().resolve_scheduled(bad_state, bad_schedule, context)
    assert rejection.outcome.status == "rejected"
    assert rejection.outcome.code == "harvest_schedule_mismatch"


def test_dead_named_recipient_is_skipped_without_stopping_finite_schedule():
    state, context, _region_id, person_id, source_id, holder_id = _state()
    entities = {
        entity_id: entity.model_copy(update={"status": "dead"}) if entity_id == person_id else entity
        for entity_id, entity in state.entities.items()
    }
    state = WorldState.model_validate({**state.model_dump(mode="json"), "entities": entities})

    resolution = _due_harvest(state, context)
    harvest = next(event for event in resolution.events if isinstance(event, RenewableResourceHarvestedEvent))
    assert harvest.items[0].recipients == []
    assert ledger_postings(harvest) == ()
    state = _apply_resolution(state, resolution)

    policy = state.entities[_region_id].components["place"]["renewable_harvests"][0]
    assert policy["last_issued_period"] == 1
    assert policy["issued_total"] == "0"
    assert state.resources[source_id].balance == Decimal("0")
    assert state.resources[holder_id].balance == Decimal("0")
    assert state.scheduled_events[0].status == "queued"
    assert state.scheduled_events[0].due_tick == 2 * PERIOD_TICKS


def test_world_advance_harvest_survives_sqlite_backup_and_continues(tmp_path):
    state, _, _, person, source, holder = _state()
    state = WorldState.model_validate({**state.model_dump(mode="json"), "current_tick": 0})
    scope = AccessScope(tenant_id=uuid4(), user_id=uuid4())
    database = Database(f"sqlite:///{(tmp_path / 'source.db').as_posix()}")
    restored_db = Database(f"sqlite:///{(tmp_path / 'restored.db').as_posix()}")
    try:
        database.create_schema()
        restored_db.create_schema()
        repo = WorldRepository(database)
        world = repo.create_world(scope, NewWorld(title="Harvest recovery", state=state))
        wid, bid = world.world.world_id, world.branch.branch_id

        def advance(repository, start, target):
            jobs = SimulationJobs(repository)
            job = jobs.enqueue_world_advance(scope, wid, bid, WorldAdvancePlan(started_tick=start, target_tick=target),
                                             idempotency_key=f"period:{target}", speed="balanced", depth="deep")
            lease = repository.claim_job("harvest", job_type="simulation.world_advance")
            for _ in range(5):
                jobs.advance(scope, lease)
                status = repository.read_job(scope, job.job_id).status
                if status != JobStatus.RUNNING:
                    break
            assert status == JobStatus.SUCCEEDED
            view = repository.read_world(scope, wid, bid)
            assert view.state.current_tick == target
            assert repository.replay(scope, wid, bid).state_hash == view.state_hash
            return view

        first = advance(repo, 0, PERIOD_TICKS)
        assert first.state.resources[source].balance == -2
        assert first.state.resources[holder].balance == 2
        backups = BackupApplication(repo)
        archive = backups.validate_archive(backups.export_world(scope, wid))
        restored = WorldRepository(restored_db)
        WorldBackupImporter(restored).import_verified_backup(scope, archive)
        assert restored.read_world(scope, wid, bid).state_hash == first.state_hash
        second = advance(restored, PERIOD_TICKS, 2 * PERIOD_TICKS)
        assert second.state.resources[source].balance == -4
        assert second.state.resources[holder].balance == 4
        assert [lot["quantity"] for lot in second.state.entities[person].components["inventory"]["lots"]] == ["2", "2"]
    finally:
        restored_db.dispose()
        database.dispose()
