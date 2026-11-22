"""SQLite integration for the persisted occupation lifecycle contracts."""

from uuid import uuid4

import pytest

from world_engine.application.backups import BackupApplication
from world_engine.domain.components import CapabilitiesComponent, PlaceComponent, PlaceRoute, SkillValue, SkillsComponent
from world_engine.domain.errors import InvalidEvent
from world_engine.domain.models import AccessScope, CommandEnvelope, NewWorld, WorldEntity, WorldState
from world_engine.domain.occupation_events import OccupationAppointedEvent, OccupationResignedEvent
from world_engine.domain.occupations import (
    OccupationSkillRequirement,
    WorkAssignment,
    WorkPosting,
)
from world_engine.rules import RuleContext, RuleEngine, parse_action
from world_engine.storage.backup_import import WorldBackupImporter
from world_engine.storage.database import Database
from world_engine.storage.repository import WorldRepository


def _fixture_state():
    actor_id, employer_id, worksite_id, remote_place_id = (uuid4() for _ in range(4))
    route_id, posting_id = uuid4(), uuid4()
    actor = WorldEntity(
        id=actor_id,
        kind="character",
        name="Mira Chen",
        location_id=worksite_id,
        components={
            "capabilities": CapabilitiesComponent(grants=["world.travel"]).model_dump(mode="json"),
            "skills": SkillsComponent(skills={
                "skill.civic_records": SkillValue(level="82", experience=120),
            }).model_dump(mode="json"),
        },
    )
    worksite = WorldEntity(
        id=worksite_id,
        kind="place",
        name="Harbor Records Office",
        components={
            "place": PlaceComponent(
                place_type="building",
                routes=[PlaceRoute(
                    route_id=route_id,
                    from_place_id=worksite_id,
                    to_place_id=remote_place_id,
                    travel_ticks=300,
                    risk="0",
                )],
            ).model_dump(mode="json"),
        },
    )
    remote_place = WorldEntity(
        id=remote_place_id,
        kind="place",
        name="North Quay",
        components={"place": PlaceComponent(place_type="settlement").model_dump(mode="json")},
    )
    employer = WorldEntity(
        id=employer_id,
        kind="organization",
        name="Harbor Cooperative",
        location_id=worksite_id,
    )
    posting = WorkPosting(
        posting_id=posting_id,
        employer_id=employer_id,
        location_id=worksite_id,
        title="Records Steward",
        required_skill=OccupationSkillRequirement(
            skill_id="skill.civic_records", minimum_level="60",
        ),
        slots=1,
        valid_from_tick=0,
        revision=4,
    )
    state = WorldState(
        player_id=actor_id,
        entities={entity.id: entity for entity in (actor, employer, worksite, remote_place)},
        work_postings=[posting],
    )
    return state, actor_id, employer_id, worksite_id, remote_place_id, route_id, posting_id


def _evaluate(state, *, world_id, branch_id, ruleset_version, actor_id, command, action):
    normalized = parse_action(action).model_dump(mode="json")
    context = RuleContext(
        world_id=world_id,
        branch_id=branch_id,
        command_id=command.command_id,
        actor_id=actor_id,
        ruleset_version=ruleset_version,
    )
    result = RuleEngine().evaluate(state, normalized, context)
    return normalized, result


def test_appointment_remote_resignation_fork_genesis_replay_and_restore(tmp_path):
    database = Database(f"sqlite:///{(tmp_path / 'occupation-source.db').as_posix()}")
    database.create_schema()
    repository = WorldRepository(database)
    restore_database = Database(f"sqlite:///{(tmp_path / 'occupation-restored.db').as_posix()}")
    restore_database.create_schema()
    restored_repository = WorldRepository(restore_database)
    try:
        (initial, actor_id, _employer_id, worksite_id, remote_place_id,
         route_id, posting_id) = _fixture_state()
        scope = AccessScope(tenant_id=uuid4(), user_id=uuid4())
        created = repository.create_world(
            scope,
            NewWorld(title="Occupation lifecycle", ruleset_version="occupation-test.v1", state=initial),
        )
        world_id, branch_id, ruleset = created.world.world_id, created.branch.branch_id, created.world.ruleset_version

        appointment_payload = {"kind": "take_job", "posting_id": str(posting_id)}
        appointment_command = CommandEnvelope(
            actor_id=actor_id,
            idempotency_key="take-records-steward-job",
            expected_state_version=0,
            command_type="action.take_job",
            payload=appointment_payload,
        )
        normalized, appointment = _evaluate(
            initial, world_id=world_id, branch_id=branch_id, ruleset_version=ruleset,
            actor_id=actor_id, command=appointment_command, action=appointment_payload,
        )
        assert normalized == appointment_payload
        assert appointment.outcome.status == "succeeded"
        assert len(appointment.events) == 1
        assert isinstance(appointment.events[0], OccupationAppointedEvent)

        # Repository validation binds the event to both the authenticated
        # command actor and its exact normalized action. Neither forged command
        # writes an event or advances the branch.
        other_actor_id = uuid4()
        wrong_actor_command = appointment_command.model_copy(update={"actor_id": other_actor_id})
        with pytest.raises(InvalidEvent, match="persisted command context"):
            repository.commit_command(scope, world_id, branch_id, wrong_actor_command, appointment.events)
        mismatched_command = appointment_command.model_copy(update={
            "payload": {"kind": "take_job", "posting_id": str(uuid4())},
        })
        with pytest.raises(InvalidEvent, match="normalized action"):
            repository.commit_command(scope, world_id, branch_id, mismatched_command, appointment.events)
        unchanged = repository.read_world(scope, world_id, branch_id)
        assert unchanged.branch.state_version == 0
        assert unchanged.last_seq == 0
        assert unchanged.state.work_assignments == []

        appointment_commit = repository.commit_command(
            scope, world_id, branch_id, appointment_command, appointment.events,
        )
        appointed = repository.read_world(scope, world_id, branch_id)
        assignment = appointed.state.work_assignments[0]
        assert assignment.actor_id == actor_id
        assert assignment.posting_id == posting_id
        assert assignment.title == "Records Steward"
        assert assignment.started_tick == 0
        assert assignment.source.reference_id == str(appointment.events[0].event_id)
        assert appointed.state.entities[actor_id].location_id == worksite_id

        # A fork at the appointment boundary retains the active assignment;
        # subsequent travel and resignation occur only on the source branch.
        employed_fork = repository.fork(
            scope, world_id, branch_id, appointment_commit.last_seq, "Employed checkpoint",
        )
        movement_payload = {
            "kind": "move",
            "destination_id": str(remote_place_id),
            "route_id": str(route_id),
        }
        movement_command = CommandEnvelope(
            actor_id=actor_id,
            idempotency_key="travel-away-from-workplace",
            expected_state_version=1,
            command_type="player.action",
            payload=movement_payload,
        )
        normalized_move, movement = _evaluate(
            appointed.state, world_id=world_id, branch_id=branch_id, ruleset_version=ruleset,
            actor_id=actor_id, command=movement_command, action=movement_payload,
        )
        assert normalized_move == movement_payload
        assert movement.outcome.status == "succeeded", movement.outcome.model_dump(mode="json")
        moved_commit = repository.commit_command(
            scope, world_id, branch_id, movement_command, movement.events,
        )
        moved = repository.read_world(scope, world_id, branch_id)
        assert moved.state.current_tick == 300
        assert moved.state.entities[actor_id].location_id == remote_place_id
        assert moved.state.work_assignments[0].ended_tick is None

        resignation_payload = {"kind": "resign_job", "assignment_id": str(assignment.assignment_id)}
        resignation_command = CommandEnvelope(
            actor_id=actor_id,
            idempotency_key="resign-records-steward-job",
            expected_state_version=moved.branch.state_version,
            command_type="action.resign_job",
            payload=resignation_payload,
        )
        normalized_resignation, resignation = _evaluate(
            moved.state, world_id=world_id, branch_id=branch_id, ruleset_version=ruleset,
            actor_id=actor_id, command=resignation_command, action=resignation_payload,
        )
        assert normalized_resignation == resignation_payload
        assert resignation.outcome.status == "succeeded"
        assert len(resignation.events) == 1
        assert isinstance(resignation.events[0], OccupationResignedEvent)
        resignation_commit = repository.commit_command(
            scope, world_id, branch_id, resignation_command, resignation.events,
        )

        resigned = repository.read_world(scope, world_id, branch_id)
        ended = resigned.state.work_assignments[0]
        assert ended.assignment_id == assignment.assignment_id
        assert ended.actor_id == actor_id
        assert ended.started_tick == 0
        assert ended.ended_tick == 300
        assert ended.revision == 2
        assert ended.source == assignment.source
        assert ended.end_source.reference_id == str(resignation.events[0].event_id)
        assert resigned.state.entities[actor_id].location_id == remote_place_id

        fork_state = repository.replay(scope, world_id, employed_fork.branch_id, from_genesis=True)
        assert fork_state.state_hash == employed_fork.state_hash
        assert fork_state.state.current_tick == 0
        assert fork_state.state.work_assignments[0].ended_tick is None
        assert repository.replay(scope, world_id, branch_id, from_genesis=True).state_hash == resignation_commit.state_hash
        audit = repository.list_events(scope, world_id, branch_id, after_seq=0)
        occupation_audit = [event for event in audit.events if event.type.startswith("core.occupation_")]
        assert [event.type for event in occupation_audit] == [
            "core.occupation_appointed.v1", "core.occupation_resigned.v1",
        ]
        assert all(event.visibility == {"policy": "author_only"} for event in occupation_audit)

        backup_app = BackupApplication(repository)
        archive = backup_app.export_world(scope, world_id)
        with backup_app.validate_archive(archive) as verified:
            restored_result = WorldBackupImporter(restored_repository).import_verified_backup(scope, verified)
            assert restored_result.world_id == world_id
            assert restored_result.branch_count == 2
        restored_root = restored_repository.replay(scope, world_id, branch_id, from_genesis=True)
        restored_fork = restored_repository.replay(scope, world_id, employed_fork.branch_id, from_genesis=True)
        assert restored_root.state_hash == resigned.state_hash
        assert restored_fork.state_hash == fork_state.state_hash
        assert restored_root.state.work_assignments[0].ended_tick == 300
        assert restored_fork.state.work_assignments[0].ended_tick is None
    finally:
        restore_database.dispose()
        database.dispose()
