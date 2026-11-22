from uuid import uuid4

import pytest

from world_engine.api.serialization import wire
from world_engine.application.backups import BackupApplication
from world_engine.application.simulation_jobs import SimulationJobs
from world_engine.domain.errors import InvalidEvent
from world_engine.domain.gameplay_events import PopulationYearAdvancedEvent, ScheduledEventCompletedEvent
from world_engine.domain.models import AccessScope, CommandEnvelope, NewWorld, TimeAdvancedEvent, WorldState
from world_engine.simulation import LongActionPlan
from world_engine.storage.backup_import import WorldBackupImporter
from world_engine.storage.database import Database
from world_engine.storage.repository import WorldRepository


def test_population_counts_keep_exact_precision_on_the_wire():
    count = 2**63 - 1
    assert wire({"population": {
        "headcount": count, "unmodeled_headcount": count - 1,
        "age_cohorts": [{"headcount": count - 1, "age_years": 30}], "carrying_capacity": None,
    }}) == {"population": {
        "headcount": str(count), "unmodeled_headcount": str(count - 1),
        "age_cohorts": [{"headcount": str(count - 1), "age_years": 30}], "carrying_capacity": None,
    }}


def test_annual_population_batch_survives_reopen_replay_and_backup(tmp_path):
    actor, group, schedule = (uuid4() for _ in range(3))
    scope = AccessScope(tenant_id=uuid4(), user_id=uuid4())
    year = 60
    population = {
        "species_id": "core.human", "headcount": 100, "unmodeled_headcount": 100,
        "last_advanced_year": 0,
        "age_rates": [
            {"age_years": age, "annual_birth_rate": "0.2" if age == 30 else "0",
             "annual_death_rate": "0.1" if age == 30 else "0"}
            for age in range(201)
        ],
        "age_cohorts": [{"age_years": age, "headcount": 100 if age == 30 else 0} for age in range(201)],
    }
    state = WorldState.model_validate({
        "player_id": actor,
        "calendar": {"day_seconds": 60, "year_days": 1},
        "entities": {
            actor: {"id": actor, "kind": "character", "name": "Visitor"},
            group: {"id": group, "kind": "group", "components": {"population": population}},
        },
        "scheduled_events": [{
            "id": schedule, "due_tick": year, "phase": "environment",
            "handler_id": "population.year_advance", "handler_version": "1.0.0",
            "repeat_ticks": year, "max_runs": 2,
        }],
    })
    url = f"sqlite:///{(tmp_path / 'world.db').as_posix()}"
    database = Database(url)
    try:
        database.create_schema()
        repo = WorldRepository(database)
        created = repo.create_world(scope, NewWorld(title="Population year", state=state))
        world_id, branch_id = created.world.world_id, created.branch.branch_id
        command = CommandEnvelope(
            actor_id=actor, idempotency_key="year-1", expected_state_version=0,
            command_type="simulation.window",
        )
        events = [
            TimeAdvancedEvent(to_tick=year),
            PopulationYearAdvancedEvent(
                year_index=1, scheduled_event_id=schedule,
                expected_populations=[{"cohort_id": group, "expected_revision": 1}],
                provenance=[{"kind": "rule", "reference_id": "population.year_advance"}],
            ),
            ScheduledEventCompletedEvent(scheduled_event_id=schedule, expected_revision=1),
        ]
        rejected = [
            events[0],
            PopulationYearAdvancedEvent(
                year_index=1, scheduled_event_id=schedule,
                expected_populations=[{"cohort_id": group, "expected_revision": 2}],
                provenance=[{"kind": "rule", "reference_id": "population.year_advance"}],
            ),
            events[2],
        ]
        with pytest.raises(InvalidEvent, match="expected revision"):
            repo.commit_command(scope, world_id, branch_id, command, rejected)
        unchanged = repo.read_world(scope, world_id, branch_id)
        assert unchanged.state.current_tick == 0 and unchanged.last_seq == 0
        assert unchanged.state.scheduled_events[0].completed_runs == 0
        first = repo.commit_command(scope, world_id, branch_id, command, events)
        repeated = repo.commit_command(scope, world_id, branch_id, command, events)
        assert repeated.duplicate and repeated.state_hash == first.state_hash
        branch = repo.fork(scope, world_id, branch_id, first.last_seq, "After first census")
    finally:
        database.dispose()

    reopened = Database(url)
    try:
        repo = WorldRepository(reopened)
        view = repo.read_world(scope, world_id, branch_id)
        projected = view.state.entities[group].components["population"]
        assert projected["headcount"] == projected["unmodeled_headcount"] == 110
        assert projected["age_cohorts"][0]["headcount"] == 20
        assert projected["age_cohorts"][31]["headcount"] == 90
        assert view.state.scheduled_events[0].due_tick == 2 * year
        assert view.last_seq == 3
        assert repo.replay(scope, world_id, branch_id).state_hash == view.state_hash
        assert repo.replay(scope, world_id, branch.branch_id).state_hash == view.state_hash
        backups = BackupApplication(repo)
        verified = backups.validate_archive(backups.export_world(scope, world_id))
        assert verified.summary()["state_hashes"] == {
            str(branch_id): view.state_hash, str(branch.branch_id): view.state_hash,
        }
    finally:
        reopened.dispose()

    restored_db = Database(f"sqlite:///{(tmp_path / 'restored.db').as_posix()}")
    try:
        restored_db.create_schema()
        restored = WorldRepository(restored_db)
        WorldBackupImporter(restored).import_verified_backup(scope, verified)
        child = restored.read_world(scope, world_id, branch.branch_id)
        assert child.state_hash == view.state_hash
        jobs = SimulationJobs(restored)
        job = jobs.enqueue(scope, world_id, branch.branch_id, LongActionPlan(
            actor_id=actor, started_tick=year, target_tick=2 * year, activity="wait",
        ), idempotency_key="year-2", speed="balanced", depth="deep")
        lease = restored.claim_job("population")
        assert jobs.advance(scope, lease).outcome == "advanced"
        annual = jobs.advance(scope, lease)
        assert annual.metrics.integration_boundary == "population_year_advanced"
        assert annual.next_tick == 2 * year
        assert jobs.advance(scope, lease).outcome == "completed"
        assert restored.read_job(scope, job.job_id).status.value == "succeeded"
        after = restored.read_world(scope, world_id, branch.branch_id)
        projected = after.state.entities[group].components["population"]
        assert projected["headcount"] == 110
        assert projected["age_cohorts"][1]["headcount"] == 20
        assert projected["age_cohorts"][32]["headcount"] == 90
        assert after.state.scheduled_events[0].status == "completed"
        assert restored.replay(scope, world_id, branch.branch_id).state_hash == after.state_hash
        assert restored.read_world(scope, world_id, branch_id).state_hash == view.state_hash
    finally:
        restored_db.dispose()

def test_sustained_catalog_fills_a_vacancy_through_real_sqlite_worker(tmp_path):
    from world_engine.application.simulation_jobs import SimulationJobs
    from world_engine.content import ContentCatalog
    from world_engine.domain.models import AccessScope, NewWorld, WorldState
    from world_engine.simulation.contracts import WorldAdvancePlan
    from world_engine.storage.database import Database
    from world_engine.storage.repository import WorldRepository
    from uuid import UUID, uuid4

    catalog = ContentCatalog()
    draft = catalog.build_world("modern_city", "Regional replenishment", "Traveler",
                                seed=0, population_mode="sustained")
    raw = draft.state.model_dump(mode="json")
    day = draft.state.calendar.day_seconds
    officeholders = set()
    for entity in raw["entities"].values():
        governance = entity["components"].get("governance")
        if governance:
            officeholders.add(governance.get("leader_id"))
            officeholders.update(office["holder_id"] for office in governance.get("offices", []))
    member_id, member = next(
        (entity_id, entity) for entity_id, entity in raw["entities"].items()
        if entity["kind"] == "character" and entity_id != raw["player_id"] and entity_id not in officeholders
    )
    life = member["components"]["life"]
    # Force one ordinary vacancy just before the first monthly harvest so this
    # integration regression exercises the real catalog without waiting decades.
    life["lifespan_ticks"] = 29 * day - life["birth_tick"]
    initial = WorldState.model_validate(raw)
    scope = AccessScope(tenant_id=uuid4(), user_id=uuid4())
    db = Database(f"sqlite:///{(tmp_path / 'replacement.db').as_posix()}")
    db.create_schema()
    try:
        repository = WorldRepository(db)
        created = repository.create_world(scope, NewWorld(
            title=draft.title, state=initial, ruleset_version=draft.ruleset_version,
            pack_version=draft.pack_version,
        ))
        world_id, branch_id = created.world.world_id, created.branch.branch_id
        jobs = SimulationJobs(repository)
        job = jobs.enqueue_world_advance(
            scope, world_id, branch_id, WorldAdvancePlan(started_tick=0, target_tick=30 * day),
            idempotency_key="replacement", speed="balanced", depth="deep",
        )
        lease = repository.claim_job("replacement", job_type="simulation.world_advance")
        assert lease is not None
        for _ in range(10):
            jobs.advance(scope, lease)
            job = repository.read_job(scope, job.job_id)
            if job.status.value != "running":
                break
        assert job.status.value == "succeeded"
        view = repository.read_world(scope, world_id, branch_id)
        historical = view.state.entities[UUID(member_id)]
        assert historical.status == "dead"
        promotions = [
            promotion
            for event in repository.list_events(scope, world_id, branch_id).events
            if event.type == "core.renewable_resource_harvested.v1"
            for promotion in event.payload.get("promotions", [])
        ]
        assert len(promotions) == 1
        assert promotions[0]["vacant_member_id"] == member_id
        replacement = view.state.entities[UUID(promotions[0]["character"]["id"])]
        assert replacement.status == "active"
        assert replacement.components["life"]["population_group_id"] == life["population_group_id"]
        assert len(replacement.components["inventory"]["lots"]) == 2
        assert repository.replay(scope, world_id, branch_id).state_hash == view.state_hash
    finally:
        db.dispose()
