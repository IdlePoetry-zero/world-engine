"""SQLite command lifecycle, branch resolution, replay and restore for ballots."""

from decimal import Decimal
from uuid import uuid4

import pytest

from world_engine.application.backups import BackupApplication
from world_engine.application.visibility import project_player_state
from world_engine.domain.components import GovernanceComponent, OfficeRecord
from world_engine.domain.events import parse_event
from world_engine.domain.leadership_ballot_events import LeadershipBallotChangedEvent
from world_engine.domain.leadership_ballot_state import BALLOT_COMMANDS
from world_engine.domain.models import AccessScope, CommandEnvelope, MembershipRole, NewWorld, WorldEntity, WorldState
from world_engine.domain.reducers import state_hash
from world_engine.rules import RuleContext, RuleEngine
from world_engine.storage.backup_import import WorldBackupImporter
from world_engine.storage.database import Database
from world_engine.storage.repository import WorldRepository


def _fixture_state(*, vacant, held_office=False):
    incumbent_id, nominee_id, voter_a_id, voter_b_id, organization_id = (uuid4() for _ in range(5))
    members = [incumbent_id, nominee_id, voter_a_id, voter_b_id]
    governance = GovernanceComponent(
        revision=6,
        members=members,
        leader_id=None if vacant else incumbent_id,
        offices=[OfficeRecord(
            office_id="office.leader", title="Leader",
            holder_id=nominee_id if held_office else (None if vacant else incumbent_id),
            authority=["organization.lead"],
        ), OfficeRecord(office_id="office.treasurer", title="Treasurer", holder_id=voter_b_id)],
        succession_policy="election",
        decision_threshold=Decimal("0.5"),
    )
    entities = {
        member_id: WorldEntity(id=member_id, kind="character", name=name, status="active")
        for member_id, name in zip(members, ("Incumbent", "Nominee", "Voter A", "Voter B"), strict=True)
    }
    entities[organization_id] = WorldEntity(
        id=organization_id,
        kind="organization",
        name="Harbor Council",
        status="active",
        components={"governance": governance.model_dump(mode="json")},
    )
    state = WorldState(player_id=incumbent_id, entities=entities)
    return state, incumbent_id, nominee_id, voter_a_id, voter_b_id, organization_id


def _commit_action(repository, scope, world_id, branch_id, actor_id, payload, *, idempotency_key):
    view = repository.read_world(scope, world_id, branch_id)
    command_id = uuid4()
    context = RuleContext(
        world_id=world_id,
        branch_id=branch_id,
        command_id=command_id,
        actor_id=actor_id,
        ruleset_version=view.world.ruleset_version,
    )
    resolution = RuleEngine().evaluate(view.state, payload, context)
    assert resolution.outcome.status == "succeeded", resolution.outcome.model_dump(mode="json")
    assert len(resolution.events) == 1
    event, = resolution.events
    assert isinstance(event, LeadershipBallotChangedEvent)
    commit = repository.commit_command(
        scope,
        world_id,
        branch_id,
        CommandEnvelope(
            command_id=command_id,
            actor_id=actor_id,
            idempotency_key=idempotency_key,
            expected_state_version=view.branch.state_version,
            command_type=BALLOT_COMMANDS[event.operation],
            payload=payload,
        ),
        resolution.events,
    )
    return commit, event


def _resolve(
    repository, scope, world_id, branch_id, incumbent_id, nominee_id, voter_a_id, voter_b_id,
    choice_pattern, *, actor_scopes,
):
    ballot_id = repository.read_world(scope, world_id, branch_id).state.leadership_ballots[0].ballot_id
    voters = (nominee_id, voter_a_id, voter_b_id)[:len(choice_pattern)]
    vote_events = []
    for index, (voter_id, choice) in enumerate(zip(voters, choice_pattern, strict=True)):
        voter_scope = actor_scopes.get(voter_id, scope)
        current = repository.read_world(voter_scope, world_id, branch_id).state.leadership_ballots[0]
        _, event = _commit_action(
            repository, voter_scope, world_id, branch_id, voter_id,
            {"kind": "cast_leadership_ballot_vote", "ballot_id": str(ballot_id),
             "expected_ballot_revision": current.revision, "choice": choice},
            idempotency_key=f"{branch_id}-vote-{index}",
        )
        vote_events.append(event)

    current = repository.read_world(scope, world_id, branch_id).state.leadership_ballots[0]
    _, close_event = _commit_action(
        repository, scope, world_id, branch_id, incumbent_id,
        {"kind": "close_leadership_ballot", "ballot_id": str(ballot_id),
         "expected_ballot_revision": current.revision},
        idempotency_key=f"{branch_id}-close",
    )
    return vote_events, close_event


def _typed_ballot_events(repository, scope, world_id, branch_id):
    records = repository.list_events(scope, world_id, branch_id, limit=100).events
    return [
        parse_event({
            "type": record.type,
            "event_id": str(record.event_id),
            "visibility": record.visibility,
            **record.payload,
        })
        for record in records
        if record.type == "core.leadership_ballot_changed.v1"
    ]


@pytest.mark.parametrize("leadership", ["existing-incumbent", "vacant-seat", "vacant-leadership-with-office-holder"])
def test_ballot_approves_or_rejects_per_branch_and_replays_restores(tmp_path, leadership):
    vacant = leadership != "existing-incumbent"
    held_office = leadership == "vacant-leadership-with-office-holder"
    source_db = Database(f"sqlite:///{(tmp_path / 'ballot-source.sqlite').as_posix()}")
    source_db.create_schema()
    restored_db = Database(f"sqlite:///{(tmp_path / 'ballot-restored.sqlite').as_posix()}")
    restored_db.create_schema()
    repository = WorldRepository(source_db)
    restored_repository = WorldRepository(restored_db)
    scope = AccessScope(tenant_id=uuid4(), user_id=uuid4())
    try:
        state, incumbent_id, nominee_id, voter_a_id, voter_b_id, organization_id = _fixture_state(
            vacant=vacant, held_office=held_office,
        )
        created = repository.create_world(scope, NewWorld(title="Leadership ballot lifecycle", state=state))
        world_id, source_branch = created.world.world_id, created.branch.branch_id
        controlled_user_id = uuid4()
        controlled_scope = AccessScope(tenant_id=scope.tenant_id, user_id=controlled_user_id)
        repository.grant_membership(
            scope, world_id, controlled_user_id, role=MembershipRole.PLAYER,
            controlled_actor_ids=[nominee_id, voter_a_id, voter_b_id],
        )
        actor_scopes = {
            nominee_id: controlled_scope,
            voter_a_id: controlled_scope,
            voter_b_id: controlled_scope,
        }

        open_operation = "open_vacant" if vacant else "open"
        open_kind = BALLOT_COMMANDS[open_operation].split(".", 1)[1]
        vote_count = 3 if vacant else 2
        projected = project_player_state(state.model_dump(mode="json"), str(incumbent_id))
        nomination_key = "vacancy_ballot_nominees" if vacant else "ballot_nominees"
        nomination = projected["own_memberships"][0]
        assert nomination["organization_id"] == str(organization_id)
        assert str(nominee_id) in {row["member_id"] for row in nomination[nomination_key]}
        if held_office:
            assert [row["member_id"] for row in nomination[nomination_key]] == [str(nominee_id)]
        opened, open_event = _commit_action(
            repository, scope, world_id, source_branch, incumbent_id,
            {"kind": open_kind, "organization_id": str(organization_id),
             "nominee_id": str(nominee_id)},
            idempotency_key="open-ballot",
        )
        assert open_event.operation == open_operation
        assert open_event.resulting_ballot.procedural_actor_id == incumbent_id
        assert open_event.resulting_ballot.vacancy_proposer_id == (incumbent_id if vacant else None)
        assert open_event.resulting_ballot.revision == 1
        assert open_event.resulting_ballot.status == "open"
        opened_state = repository.read_world(scope, world_id, source_branch).state.model_dump(mode="json")
        open_projection = project_player_state(opened_state, str(incumbent_id))
        assert "vacancy_ballot_nominees" not in open_projection["own_memberships"][0]
        own_ballot = open_projection["own_leadership_ballots"][0]
        assert own_ballot.get("is_vacancy_proposer", False) is vacant
        assert own_ballot["is_incumbent"] is not vacant
        assert str(incumbent_id) not in {row["voter_id"] for row in own_ballot["waiting_voters"]}
        alternate = repository.fork(
            scope, world_id, source_branch, opened.last_seq, "Before leadership vote",
        )

        approved_votes, approved_event = _resolve(
            repository, scope, world_id, source_branch, incumbent_id, nominee_id,
            voter_a_id, voter_b_id, ("yes",) * vote_count, actor_scopes=actor_scopes,
        )
        rejected_votes, rejected_event = _resolve(
            repository, scope, world_id, alternate.branch_id, incumbent_id, nominee_id,
            voter_a_id, voter_b_id, ("no", "no"), actor_scopes=actor_scopes,
        )
        assert [event.resulting_ballot.status for event in approved_votes] == ["open"] * vote_count
        assert [event.resulting_ballot.status for event in rejected_votes] == ["open", "open"]
        assert approved_event.operation == rejected_event.operation == "close"
        assert approved_event.resulting_ballot.status == "approved"
        assert rejected_event.resulting_ballot.status == "rejected"

        source_head = repository.read_world(scope, world_id, source_branch)
        alternate_head = repository.read_world(scope, world_id, alternate.branch_id)
        assert source_head.state_hash == repository.replay(
            scope, world_id, source_branch, from_genesis=True,
        ).state_hash
        assert alternate_head.state_hash == repository.replay(
            scope, world_id, alternate.branch_id, from_genesis=True,
        ).state_hash
        source_governance = source_head.state.entities[organization_id].components["governance"]
        alternate_governance = alternate_head.state.entities[organization_id].components["governance"]
        assert source_governance["leader_id"] == str(nominee_id)
        assert source_governance["offices"][0]["holder_id"] == str(nominee_id)
        original_leader = None if vacant else str(incumbent_id)
        original_holder = str(nominee_id) if held_office else original_leader
        assert alternate_governance["leader_id"] == original_leader
        assert alternate_governance["offices"][0]["holder_id"] == original_holder
        if held_office:
            assert source_governance["offices"][0] == alternate_governance["offices"][0]
        assert source_governance["offices"][1] == alternate_governance["offices"][1]
        assert source_head.state.leadership_ballots[0].status == "approved"
        assert alternate_head.state.leadership_ballots[0].status == "rejected"

        source_typed_events = _typed_ballot_events(repository, scope, world_id, source_branch)
        expected_operations = [open_operation, *(["cast"] * vote_count), "close"]
        assert len(source_typed_events) == len(expected_operations)
        assert all(isinstance(event, LeadershipBallotChangedEvent) for event in source_typed_events)
        assert all(event.visibility == {"policy": "author_only"} for event in source_typed_events)
        assert [event.operation for event in source_typed_events] == expected_operations

        backup_app = BackupApplication(repository)
        archive = backup_app.export_world(scope, world_id)
        with backup_app.validate_archive(archive) as verified:
            imported = WorldBackupImporter(restored_repository).import_verified_backup(scope, verified)
        assert imported.world_id == world_id
        assert imported.branch_count == 2
        restored_source = restored_repository.read_world(scope, world_id, source_branch)
        restored_alternate = restored_repository.read_world(scope, world_id, alternate.branch_id)
        assert restored_source.state_hash == source_head.state_hash == state_hash(source_head.state)
        assert restored_alternate.state_hash == alternate_head.state_hash == state_hash(alternate_head.state)
        assert restored_repository.replay(scope, world_id, source_branch, from_genesis=True).state_hash == source_head.state_hash
        assert restored_repository.replay(scope, world_id, alternate.branch_id, from_genesis=True).state_hash == alternate_head.state_hash
        assert restored_source.state.leadership_ballots[0].status == "approved"
        assert restored_alternate.state.leadership_ballots[0].status == "rejected"
        assert [event.operation for event in _typed_ballot_events(
            restored_repository, scope, world_id, source_branch,
        )] == expected_operations
    finally:
        restored_db.dispose()
        source_db.dispose()
