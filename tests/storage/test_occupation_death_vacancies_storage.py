"""SQLite coverage for automatic job vacancy closure on worker death."""

import io
from uuid import uuid4

import pytest

from world_engine.application.backups import BackupApplication
from world_engine.domain.components import LifeComponent, PlaceComponent, SkillValue, SkillsComponent
from world_engine.domain.events import parse_event
from world_engine.domain.gameplay_events import LifeEndedEvent
from world_engine.domain.models import AccessScope, CommandEnvelope, NewWorld, TimeAdvancedEvent, WorldEntity, WorldState
from world_engine.domain.occupation_death_vacancies import OccupationDeathVacanciesResolvedEvent
from world_engine.domain.occupation_events import OccupationAppointedEvent
from world_engine.domain.occupations import OccupationSkillRequirement, WorkPosting
from world_engine.rules import RuleContext, RuleEngine, parse_action
from world_engine.storage.backup_import import WorldBackupImporter
from world_engine.storage.database import Database
from world_engine.storage.repository import WorldRepository


def _character(entity_id, name, workplace_id, *, lifespan_ticks=None):
    components = {
        "skills": SkillsComponent(skills={
            "skill.civic_records": SkillValue(level="82", experience=120),
        }).model_dump(mode="json"),
    }
    if lifespan_ticks is not None:
        components["life"] = LifeComponent(birth_tick=0, lifespan_ticks=lifespan_ticks).model_dump(mode="json")
    return WorldEntity(
        id=entity_id,
        kind="character",
        name=name,
        location_id=workplace_id,
        components=components,
    )


def _evaluate(state, *, world_id, branch_id, ruleset_version, actor_id, command, action):
    normalized = parse_action(action).model_dump(mode="json")
    context = RuleContext(
        world_id=world_id,
        branch_id=branch_id,
        command_id=command.command_id,
        actor_id=actor_id,
        ruleset_version=ruleset_version,
    )
    return normalized, RuleEngine().evaluate(state, normalized, context)


@pytest.mark.parametrize("archive_version", ["1.0", "2.0"])
def test_worker_death_closes_assignment_frees_posting_replays_and_restores(tmp_path, archive_version):
    database = Database(f"sqlite:///{(tmp_path / 'occupation-death-source.db').as_posix()}")
    database.create_schema()
    repository = WorldRepository(database)
    restore_database = Database(f"sqlite:///{(tmp_path / 'occupation-death-restored.db').as_posix()}")
    restore_database.create_schema()
    restored_repository = WorldRepository(restore_database)
    try:
        player_id, worker_id, employer_id, workplace_id, posting_id = (uuid4() for _ in range(5))
        workplace = WorldEntity(
            id=workplace_id,
            kind="place",
            name="Harbor Records Office",
            components={"place": PlaceComponent(place_type="building").model_dump(mode="json")},
        )
        employer = WorldEntity(
            id=employer_id,
            kind="organization",
            name="Harbor Cooperative",
            location_id=workplace_id,
        )
        player = _character(player_id, "Mira Chen", workplace_id)
        worker = _character(worker_id, "Jun Li", workplace_id, lifespan_ticks=10)
        posting = WorkPosting(
            posting_id=posting_id,
            employer_id=employer_id,
            location_id=workplace_id,
            title="Records Steward",
            required_skill=OccupationSkillRequirement(
                skill_id="skill.civic_records", minimum_level="60",
            ),
            slots=1,
            valid_from_tick=0,
            revision=4,
        )
        initial = WorldState(
            player_id=player_id,
            entities={entity.id: entity for entity in (player, worker, employer, workplace)},
            work_postings=[posting],
        )
        scope = AccessScope(tenant_id=uuid4(), user_id=uuid4())
        created = repository.create_world(
            scope,
            NewWorld(
                title="Occupation death vacancy",
                ruleset_version="occupation-death-test.v1",
                state=initial,
            ),
        )
        world_id, branch_id = created.world.world_id, created.branch.branch_id
        ruleset_version = created.world.ruleset_version

        # First take the only slot through the normal typed occupation rule.
        take_payload = {"kind": "take_job", "posting_id": str(posting_id)}
        take_command = CommandEnvelope(
            actor_id=worker_id,
            idempotency_key="worker-takes-records-job",
            expected_state_version=0,
            command_type="action.take_job",
            payload=take_payload,
        )
        normalized, take_resolution = _evaluate(
            initial,
            world_id=world_id,
            branch_id=branch_id,
            ruleset_version=ruleset_version,
            actor_id=worker_id,
            command=take_command,
            action=take_payload,
        )
        assert normalized == take_payload
        assert take_resolution.outcome.status == "succeeded"
        assert len(take_resolution.events) == 1
        assert isinstance(take_resolution.events[0], OccupationAppointedEvent)
        take_commit = repository.commit_command(
            scope, world_id, branch_id, take_command, take_resolution.events,
        )
        appointed = repository.read_world(scope, world_id, branch_id)
        assignment = appointed.state.work_assignments[0]
        assert assignment.actor_id == worker_id
        assert assignment.posting_id == posting_id
        assert assignment.ended_tick is None
        assert assignment.source.reference_id == str(take_resolution.events[0].event_id)
        assert appointed.branch.state_version == 1

        # The death command contains only authoritative time and lifespan
        # events. Repository commit appends the typed vacancy closure suffix.
        death_event = LifeEndedEvent(
            entity_id=worker_id,
            expected_life_revision=1,
        )
        death_command = CommandEnvelope(
            actor_id=player_id,
            idempotency_key="expire-employed-worker",
            expected_state_version=take_commit.state_version,
            command_type="simulation.death",
        )
        death_events = [TimeAdvancedEvent(to_tick=10), death_event]
        death_commit = repository.commit_command(
            scope, world_id, branch_id, death_command, death_events,
        )
        assert [event.type for event in death_commit.events] == [
            "core.time_advanced.v1",
            "core.life_ended.v1",
            "core.occupation_death_vacancies_resolved.v1",
        ]
        vacancy_record = death_commit.events[-1]
        assert vacancy_record.commit_boundary is True
        assert vacancy_record.visibility == {"policy": "author_only"}
        vacancy_event = parse_event({
            "type": vacancy_record.type,
            "event_id": vacancy_record.event_id,
            "visibility": vacancy_record.visibility,
            **vacancy_record.payload,
        })
        assert isinstance(vacancy_event, OccupationDeathVacanciesResolvedEvent)
        assert vacancy_event.effective_tick == 10
        assert len(vacancy_event.closures) == 1
        closure = vacancy_event.closures[0]
        assert closure.assignment_id == assignment.assignment_id
        assert closure.actor_id == worker_id
        assert closure.terminal_event_id == death_event.event_id
        assert closure.expected_assignment_revision == 1

        dead_view = repository.read_world(scope, world_id, branch_id)
        ended = dead_view.state.work_assignments[0]
        assert dead_view.state.entities[worker_id].status == "dead"
        assert dead_view.state.current_tick == 10
        assert ended.assignment_id == assignment.assignment_id
        assert ended.actor_id == worker_id
        assert ended.started_tick == 0
        assert ended.ended_tick == 10
        assert ended.revision == 2
        assert ended.source == assignment.source
        assert ended.end_source.kind == "event"
        assert ended.end_source.reference_id == str(death_event.event_id)

        # The idempotent retry returns the original three-event receipt and
        # does not derive or append a second closure.
        repeated_death = repository.commit_command(
            scope, world_id, branch_id, death_command, death_events,
        )
        assert repeated_death.duplicate is True
        assert [event.event_id for event in repeated_death.events] == [
            event.event_id for event in death_commit.events
        ]
        assert repository.read_world(scope, world_id, branch_id).branch.state_version == death_commit.state_version

        # The now-vacant slot can be filled by a different, qualified actor.
        player_take_command = CommandEnvelope(
            actor_id=player_id,
            idempotency_key="player-takes-freed-records-job",
            expected_state_version=death_commit.state_version,
            command_type="action.take_job",
            payload=take_payload,
        )
        normalized_player_take, player_take = _evaluate(
            dead_view.state,
            world_id=world_id,
            branch_id=branch_id,
            ruleset_version=ruleset_version,
            actor_id=player_id,
            command=player_take_command,
            action=take_payload,
        )
        assert normalized_player_take == take_payload
        assert player_take.outcome.status == "succeeded", player_take.outcome.model_dump(mode="json")
        assert len(player_take.events) == 1
        player_take_commit = repository.commit_command(
            scope, world_id, branch_id, player_take_command, player_take.events,
        )
        final = repository.read_world(scope, world_id, branch_id)
        assert final.branch.state_version == player_take_commit.state_version
        assert len(final.state.work_assignments) == 2
        player_assignment = next(item for item in final.state.work_assignments if item.actor_id == player_id)
        assert player_assignment.posting_id == posting_id
        assert player_assignment.ended_tick is None
        assert player_assignment.started_tick == 10
        assert player_assignment.source.reference_id == str(player_take.events[0].event_id)

        replayed = repository.replay(scope, world_id, branch_id, from_genesis=True)
        assert replayed.state_hash == final.state_hash
        assert replayed.state.work_assignments == final.state.work_assignments
        assert replayed.state.entities[worker_id].status == "dead"

        backup_app = BackupApplication(repository)
        archive = backup_app.export_world(scope, world_id, format_version=archive_version)
        verified_archive = (
            backup_app.validate_archive_file_streaming(io.BytesIO(archive))
            if archive_version == "2.0"
            else backup_app.validate_archive(archive)
        )
        with verified_archive as verified:
            restored_result = WorldBackupImporter(restored_repository).import_verified_backup(scope, verified)
            assert restored_result.world_id == world_id
            assert restored_result.branch_count == 1
        restored = restored_repository.replay(scope, world_id, branch_id, from_genesis=True)
        assert restored.state_hash == final.state_hash
        assert restored.state.work_assignments == final.state.work_assignments
        assert restored.state.entities[worker_id].status == "dead"
    finally:
        restore_database.dispose()
        database.dispose()
