"""SQLite coverage for owner repair of stale historical job vacancies."""

import pytest
from uuid import uuid4

from world_engine.application.backups import BackupApplication
from world_engine.domain.components import PlaceComponent, SkillValue, SkillsComponent
from world_engine.domain.errors import AccessDenied
from world_engine.domain.events import parse_event
from world_engine.domain.models import AccessScope, CommandEnvelope, MembershipRole, NewWorld, WorldEntity, WorldState
from world_engine.domain.occupation_legacy_vacancies import OccupationLegacyVacanciesResolvedEvent
from world_engine.domain.occupation_events import OccupationAppointedEvent
from world_engine.domain.occupations import (
    OccupationSkillRequirement,
    OccupationSource,
    WorkAssignment,
    WorkPosting,
    occupation_assignment_id,
)
from world_engine.rules import RuleContext, RuleEngine, parse_action
from world_engine.storage.backup_import import WorldBackupImporter
from world_engine.storage.database import Database
from world_engine.storage.repository import WorldRepository


def _character(entity_id, name, workplace_id):
    return WorldEntity(
        id=entity_id,
        kind="character",
        name=name,
        location_id=workplace_id,
        components={"skills": SkillsComponent(skills={
            "skill.civic_records": SkillValue(level="82", experience=120),
        }).model_dump(mode="json")},
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


def test_owner_settles_legacy_dead_worker_vacancy_idempotently_and_restores(tmp_path):
    database = Database(f"sqlite:///{(tmp_path / 'occupation-legacy-source.db').as_posix()}")
    database.create_schema()
    repository = WorldRepository(database)
    restore_database = Database(f"sqlite:///{(tmp_path / 'occupation-legacy-restored.db').as_posix()}")
    restore_database.create_schema()
    restored_repository = WorldRepository(restore_database)
    try:
        tick = 75
        player_id, dead_worker_id, employer_id, workplace_id, posting_id = (uuid4() for _ in range(5))
        player = _character(player_id, "Mira Chen", workplace_id)
        dead_worker = _character(dead_worker_id, "Jun Li", workplace_id).model_copy(update={"status": "dead"})
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
        genesis_source = OccupationSource(kind="genesis", reference_id="legacy-snapshot:worker-assignment")
        stale_assignment = WorkAssignment(
            assignment_id=occupation_assignment_id(genesis_source, dead_worker_id, posting_id),
            actor_id=dead_worker_id,
            posting_id=posting_id,
            employer_id=employer_id,
            location_id=workplace_id,
            title=posting.title,
            required_skill=posting.required_skill,
            started_tick=0,
            revision=1,
            source=genesis_source,
        )
        initial = WorldState(
            current_tick=tick,
            player_id=player_id,
            entities={entity.id: entity for entity in (player, dead_worker, employer, workplace)},
            work_postings=[posting],
            work_assignments=[stale_assignment],
        )
        owner_scope = AccessScope(tenant_id=uuid4(), user_id=uuid4())
        player_scope = AccessScope(tenant_id=owner_scope.tenant_id, user_id=uuid4())
        created = repository.create_world(
            owner_scope,
            NewWorld(
                title="Legacy occupation vacancy",
                ruleset_version="occupation-legacy-vacancy-test.v1",
                state=initial,
            ),
        )
        world_id, branch_id = created.world.world_id, created.branch.branch_id
        ruleset_version = created.world.ruleset_version
        repository.grant_membership(
            owner_scope,
            world_id,
            player_scope.user_id,
            MembershipRole.PLAYER,
            controlled_actor_ids=[player_id],
        )

        # A normal player cannot invoke this author-only maintenance operation.
        with pytest.raises(AccessDenied):
            repository.settle_legacy_occupation_vacancies(
                player_scope,
                world_id,
                branch_id,
                command_id=uuid4(),
                expected_state_version=0,
            )

        maintenance_command_id = uuid4()
        maintenance = repository.settle_legacy_occupation_vacancies(
            owner_scope,
            world_id,
            branch_id,
            command_id=maintenance_command_id,
            expected_state_version=0,
        )
        assert maintenance is not None
        assert maintenance.duplicate is False
        assert maintenance.state_version == 1
        assert len(maintenance.events) == 1
        vacancy_record = maintenance.events[0]
        assert vacancy_record.type == "core.occupation_legacy_vacancies_resolved.v1"
        assert vacancy_record.tick == tick
        assert vacancy_record.commit_boundary is True
        assert vacancy_record.visibility == {"policy": "author_only"}
        vacancy_event = parse_event({
            "type": vacancy_record.type,
            "event_id": vacancy_record.event_id,
            "visibility": vacancy_record.visibility,
            **vacancy_record.payload,
        })
        assert isinstance(vacancy_event, OccupationLegacyVacanciesResolvedEvent)
        assert vacancy_event.command_id == maintenance_command_id
        assert vacancy_event.effective_tick == tick
        assert len(vacancy_event.vacancies) == 1
        vacancy = vacancy_event.vacancies[0]
        assert vacancy.assignment_id == stale_assignment.assignment_id
        assert vacancy.actor_id == dead_worker_id
        assert vacancy.expected_assignment_revision == 1

        settled = repository.read_world(owner_scope, world_id, branch_id)
        ended = settled.state.work_assignments[0]
        assert ended.assignment_id == stale_assignment.assignment_id
        assert ended.ended_tick == tick
        assert ended.revision == 2
        assert ended.source == genesis_source
        assert ended.end_source.kind == "event"
        assert ended.end_source.reference_id == str(vacancy_event.event_id)

        # Reusing the exact maintenance command returns its receipt; a fresh
        # command against a clean version is a true no-op.
        duplicate = repository.settle_legacy_occupation_vacancies(
            owner_scope,
            world_id,
            branch_id,
            command_id=maintenance_command_id,
            expected_state_version=0,
        )
        assert duplicate is not None and duplicate.duplicate is True
        assert [event.event_id for event in duplicate.events] == [event.event_id for event in maintenance.events]
        no_op = repository.settle_legacy_occupation_vacancies(
            owner_scope,
            world_id,
            branch_id,
            command_id=uuid4(),
            expected_state_version=maintenance.state_version,
        )
        assert no_op is None
        assert repository.read_world(owner_scope, world_id, branch_id).branch.state_version == maintenance.state_version

        # The formerly occupied single slot can now be taken by the player.
        take_payload = {"kind": "take_job", "posting_id": str(posting_id)}
        take_command = CommandEnvelope(
            actor_id=player_id,
            idempotency_key="player-takes-legacy-vacancy",
            expected_state_version=maintenance.state_version,
            command_type="action.take_job",
            payload=take_payload,
        )
        normalized, take = _evaluate(
            settled.state,
            world_id=world_id,
            branch_id=branch_id,
            ruleset_version=ruleset_version,
            actor_id=player_id,
            command=take_command,
            action=take_payload,
        )
        assert normalized == take_payload
        assert take.outcome.status == "succeeded", take.outcome.model_dump(mode="json")
        assert len(take.events) == 1
        rehired = repository.commit_command(
            player_scope, world_id, branch_id, take_command, take.events,
        )
        final = repository.read_world(owner_scope, world_id, branch_id)
        player_assignment = next(row for row in final.state.work_assignments if row.actor_id == player_id)
        assert player_assignment.ended_tick is None
        assert player_assignment.started_tick == tick
        assert rehired.state_version == maintenance.state_version + 1

        replayed = repository.replay(owner_scope, world_id, branch_id, from_genesis=True)
        assert replayed.state_hash == final.state_hash
        assert replayed.state.work_assignments == final.state.work_assignments

        backup_app = BackupApplication(repository)
        archive = backup_app.export_world(owner_scope, world_id)
        with backup_app.validate_archive(archive) as verified:
            imported = WorldBackupImporter(restored_repository).import_verified_backup(owner_scope, verified)
            assert imported.world_id == world_id
            assert imported.branch_count == 1
        restored = restored_repository.replay(owner_scope, world_id, branch_id, from_genesis=True)
        assert restored.state_hash == final.state_hash
        assert restored.state.work_assignments == final.state.work_assignments
    finally:
        restore_database.dispose()
        database.dispose()
