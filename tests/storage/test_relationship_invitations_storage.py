"""SQLite lifecycle, branch privacy, replay and restore for invitations."""

from uuid import uuid4

import pytest

from world_engine.application.backups import BackupApplication
from world_engine.application.visibility import project_event, project_player_state
from world_engine.domain.components import LifeComponent
from world_engine.domain.errors import AccessDenied, InvalidEvent
from world_engine.domain.models import (
    AccessScope,
    CommandEnvelope,
    MembershipRole,
    NewWorld,
    TimeAdvancedEvent,
    WorldEntity,
    WorldState,
)
from world_engine.domain.relationship_invitation_events import (
    RelationInvitationChangedEvent,
    invitation_event_id,
    participant_visibility,
)
from world_engine.domain.relationship_invitation_state import NPC_ATTENDANCE_COMMAND
from world_engine.domain.relationship_invitations import (
    InvitationMeeting,
    attend_relationship_meeting,
)
from world_engine.rules import RuleContext, RuleEngine
from world_engine.storage.backup_import import WorldBackupImporter
from world_engine.storage.database import Database
from world_engine.storage.repository import WorldRepository


def _fixture_state():
    player_id, first_id, second_id, stranger_id, place_id = (uuid4() for _ in range(5))
    state = WorldState()
    year_ticks = state.calendar.day_seconds * state.calendar.year_days
    adult_life = LifeComponent(
        birth_tick=-(20 * year_ticks),
        lifespan_ticks=80 * year_ticks,
        maturity_ticks=18 * year_ticks,
        life_stage="adult",
    ).model_dump(mode="json")

    def person(entity_id, name):
        return WorldEntity(
            id=entity_id,
            kind="character",
            name=name,
            status="active",
            location_id=place_id,
            components={"life": adult_life},
        )

    fixture = WorldState(
        current_tick=0,
        player_id=player_id,
        entities={
            place_id: WorldEntity(
                id=place_id,
                kind="place",
                name="Riverside Courtyard",
                status="active",
            ),
            player_id: person(player_id, "Lin Yue"),
            first_id: person(first_id, "Anwen Cairn"),
            second_id: person(second_id, "Beren Alder"),
            stranger_id: person(stranger_id, "Fenn Alder"),
        },
    )
    return fixture, player_id, first_id, second_id, stranger_id


def _commit_action(repository, scope, world_id, branch_id, actor_id, kind, payload, *, key):
    view = repository.read_world(scope, world_id, branch_id)
    command_id = uuid4()
    normalized = {"kind": kind, **payload}
    context = RuleContext(
        world_id=world_id,
        branch_id=branch_id,
        command_id=command_id,
        actor_id=actor_id,
        ruleset_version=view.world.ruleset_version,
    )
    resolution = RuleEngine().evaluate(view.state, normalized, context)
    assert resolution.outcome.status == "succeeded", resolution.outcome.model_dump(mode="json")
    assert len(resolution.events) == 1
    event, = resolution.events
    assert isinstance(event, RelationInvitationChangedEvent)
    command = CommandEnvelope(
        command_id=command_id,
        actor_id=actor_id,
        idempotency_key=key,
        expected_state_version=view.branch.state_version,
        command_type=f"action.{kind}",
        payload=normalized,
    )
    commit = repository.commit_command(scope, world_id, branch_id, command, resolution.events)
    return commit, event


def test_invitation_proposals_responses_are_private_branch_local_and_restore(tmp_path):
    source_db = Database(f"sqlite:///{(tmp_path / 'invitations-source.db').as_posix()}")
    source_db.create_schema()
    restored_db = Database(f"sqlite:///{(tmp_path / 'invitations-restored.db').as_posix()}")
    restored_db.create_schema()
    repository = WorldRepository(source_db)
    restored_repository = WorldRepository(restored_db)
    scope = AccessScope(tenant_id=uuid4(), user_id=uuid4())
    try:
        fixture, player_id, first_id, second_id, stranger_id = _fixture_state()
        created = repository.create_world(
            scope,
            NewWorld(title="Invitation lifecycle", ruleset_version="invitation-storage-test.v1", state=fixture),
        )
        world_id, branch_id = created.world.world_id, created.branch.branch_id

        first_proposal, first_event = _commit_action(
            repository,
            scope,
            world_id,
            branch_id,
            player_id,
            "propose_relationship_invitation",
            {"target_id": str(first_id)},
            key="propose-first-invitation",
        )
        second_proposal, second_event = _commit_action(
            repository,
            scope,
            world_id,
            branch_id,
            player_id,
            "propose_relationship_invitation",
            {"target_id": str(second_id)},
            key="propose-second-invitation",
        )
        proposed = repository.read_world(scope, world_id, branch_id)
        invitations = proposed.state.relationship_invitations
        assert len(invitations) == 2
        assert len({item.invitation_id for item in invitations}) == 2
        assert {item.status for item in invitations} == {"pending"}
        first_invitation = next(item for item in invitations if item.invitee_id == first_id)
        second_invitation = next(item for item in invitations if item.invitee_id == second_id)
        assert first_event.resulting_invitation.invitation_id == first_invitation.invitation_id
        assert second_event.resulting_invitation.invitation_id == second_invitation.invitation_id
        assert first_proposal.state_version == 1 and second_proposal.state_version == 2

        # The proposer and each invitee see only their own invitation records;
        # a co-located stranger receives none.
        raw_projection = proposed.state.model_dump(mode="json")
        assert len(project_player_state(raw_projection, str(player_id))["own_relationship_invitations"]) == 2
        assert [item["invitation_id"] for item in project_player_state(
            raw_projection, str(first_id),
        )["own_relationship_invitations"]] == [str(first_invitation.invitation_id)]
        assert [item["invitation_id"] for item in project_player_state(
            raw_projection, str(second_id),
        )["own_relationship_invitations"]] == [str(second_invitation.invitation_id)]
        assert project_player_state(raw_projection, str(stranger_id))["own_relationship_invitations"] == []

        # The proposer cannot accept on another character's behalf. The failed
        # rule result emits nothing and leaves the committed head unchanged.
        before_wrong_actor = proposed.branch.state_version
        wrong_actor_context = RuleContext(
            world_id=world_id,
            branch_id=branch_id,
            command_id=uuid4(),
            actor_id=player_id,
            ruleset_version=proposed.world.ruleset_version,
        )
        wrong_actor = RuleEngine().evaluate(
            proposed.state,
            {"kind": "resolve_relationship_invitation", "invitation_id": str(first_invitation.invitation_id),
             "operation": "accept"},
            wrong_actor_context,
        )
        assert wrong_actor.outcome.status == "rejected"
        assert wrong_actor.events == []
        assert repository.read_world(scope, world_id, branch_id).branch.state_version == before_wrong_actor

        # Resolve one invitee's choice, then fork before the second participant
        # responds so the source and alternate branches can exercise different
        # outcomes from the same exact pending snapshot.
        accepted_commit, accepted_event = _commit_action(
            repository,
            scope,
            world_id,
            branch_id,
            first_id,
            "resolve_relationship_invitation",
            {"invitation_id": str(first_invitation.invitation_id), "operation": "accept"},
            key="accept-first-invitation",
        )
        assert accepted_event.operation == "accept"
        before_second_choice = repository.read_world(scope, world_id, branch_id)
        assert next(item for item in before_second_choice.state.relationship_invitations
                    if item.invitation_id == first_invitation.invitation_id).status == "accepted"
        assert next(item for item in before_second_choice.state.relationship_invitations
                    if item.invitation_id == second_invitation.invitation_id).status == "pending"
        alternate = repository.fork(
            scope, world_id, branch_id, accepted_commit.last_seq, "Before second invitation response",
        )

        declined_commit, declined_event = _commit_action(
            repository,
            scope,
            world_id,
            branch_id,
            second_id,
            "resolve_relationship_invitation",
            {"invitation_id": str(second_invitation.invitation_id), "operation": "decline"},
            key="decline-second-invitation",
        )
        accepted_alternate, alternate_event = _commit_action(
            repository,
            scope,
            world_id,
            alternate.branch_id,
            second_id,
            "resolve_relationship_invitation",
            {"invitation_id": str(second_invitation.invitation_id), "operation": "accept"},
            key="accept-second-invitation-alternate",
        )
        assert declined_event.operation == "decline"
        assert alternate_event.operation == "accept"

        source_head = repository.read_world(scope, world_id, branch_id)
        alternate_head = repository.read_world(scope, world_id, alternate.branch_id)
        source_invitations = {item.invitee_id: item for item in source_head.state.relationship_invitations}
        alternate_invitations = {item.invitee_id: item for item in alternate_head.state.relationship_invitations}
        assert source_invitations[first_id].status == alternate_invitations[first_id].status == "accepted"
        assert source_invitations[second_id].status == "declined"
        assert alternate_invitations[second_id].status == "accepted"
        assert declined_commit.state_version == 4
        assert accepted_alternate.state_version == 4
        assert source_head.state.relations == alternate_head.state.relations == []
        assert source_head.state.current_tick == alternate_head.state.current_tick == 0

        # Event history uses the same participant-only audience; no stranger
        # receives a historical copy of this invitation change.
        audit = repository.list_events(scope, world_id, branch_id, limit=100)
        invitation_events = [item for item in audit.events if item.type == "core.relationship_invitation_changed.v1"]
        assert len(invitation_events) == 4
        assert all(item.visibility["policy"] == "subject_ids" for item in invitation_events)
        assert all(len(item.visibility["subject_ids"]) == 2 for item in invitation_events)
        assert all(project_event({
            "event_id": str(item.event_id), "type": item.type, "tick": str(item.tick),
            "sequence": str(item.sequence), "visibility": item.visibility, "payload": item.payload,
        }, str(stranger_id)) is None for item in invitation_events)

        assert repository.replay(scope, world_id, branch_id, from_genesis=True).state_hash == source_head.state_hash
        assert repository.replay(scope, world_id, alternate.branch_id, from_genesis=True).state_hash == alternate_head.state_hash

        backup_app = BackupApplication(repository)
        archive = backup_app.export_world(scope, world_id)
        with backup_app.validate_archive(archive) as verified:
            result = WorldBackupImporter(restored_repository).import_verified_backup(scope, verified)
            assert result.world_id == world_id
            assert result.branch_count == 2
        restored_source = restored_repository.read_world(scope, world_id, branch_id)
        restored_alternate = restored_repository.read_world(scope, world_id, alternate.branch_id)
        assert restored_source.state_hash == source_head.state_hash
        assert restored_alternate.state_hash == alternate_head.state_hash
        assert restored_repository.replay(scope, world_id, branch_id, from_genesis=True).state_hash == source_head.state_hash
        assert restored_repository.replay(scope, world_id, alternate.branch_id, from_genesis=True).state_hash == alternate_head.state_hash
        assert {item.invitee_id: item.status for item in restored_source.state.relationship_invitations} == {
            first_id: "accepted", second_id: "declined",
        }
        assert {item.invitee_id: item.status for item in restored_alternate.state.relationship_invitations} == {
            first_id: "accepted", second_id: "accepted",
        }
    finally:
        restored_db.dispose()
        source_db.dispose()


def test_requested_npc_attendance_requires_work_check_and_hosted_actor_authorization(tmp_path):
    database = Database(f"sqlite:///{(tmp_path / 'requested-attendance.db').as_posix()}")
    database.create_schema()
    repository = WorldRepository(database)
    owner_scope = AccessScope(tenant_id=uuid4(), user_id=uuid4())
    try:
        fixture, player_id, npc_id, _, _ = _fixture_state()
        created = repository.create_world(
            owner_scope,
            NewWorld(title="Requested attendance", ruleset_version="requested-attendance.v1", state=fixture),
        )
        world_id, branch_id = created.world.world_id, created.branch.branch_id
        _, proposal = _commit_action(
            repository,
            owner_scope,
            world_id,
            branch_id,
            player_id,
            "propose_relationship_invitation",
            {
                "target_id": str(npc_id),
                "meeting": {"start_tick": 100, "end_tick": 200},
            },
            key="requested-attendance-proposal",
        )
        invitation = proposal.resulting_invitation
        _, _ = _commit_action(
            repository,
            owner_scope,
            world_id,
            branch_id,
            npc_id,
            "resolve_relationship_invitation",
            {"invitation_id": str(invitation.invitation_id), "operation": "accept"},
            key="requested-attendance-acceptance",
        )
        accepted_view = repository.read_world(owner_scope, world_id, branch_id)
        time_command = CommandEnvelope(
            actor_id=player_id,
            idempotency_key="requested-attendance-meeting-start",
            expected_state_version=accepted_view.branch.state_version,
            command_type="wait",
        )
        repository.commit_command(
            owner_scope,
            world_id,
            branch_id,
            time_command,
            [TimeAdvancedEvent(to_tick=150)],
        )
        state = repository.read_world(owner_scope, world_id, branch_id).state
        command_id = uuid4()
        attended = attend_relationship_meeting(
            state,
            state.relationship_invitations,
            invitation_id=invitation.invitation_id,
            actor_id=npc_id,
            expected_revision=2,
            check_work_calendar=True,
        )
        event = RelationInvitationChangedEvent(
            event_id=invitation_event_id(command_id, created.world.ruleset_version, "attend", invitation.invitation_id),
            world_id=world_id,
            branch_id=branch_id,
            command_id=command_id,
            actor_id=npc_id,
            requested_by=player_id,
            work_calendar_checked=True,
            ruleset_version=created.world.ruleset_version,
            operation="attend",
            expected_revision=2,
            resulting_invitation=attended,
            visibility=participant_visibility(player_id, npc_id),
        )
        payload = {
            "kind": "request_relationship_meeting_attendance",
            "invitation_id": str(invitation.invitation_id),
        }
        command = CommandEnvelope(
            command_id=command_id,
            actor_id=player_id,
            idempotency_key="requested-npc-attendance",
            expected_state_version=3,
            command_type=NPC_ATTENDANCE_COMMAND,
            payload=payload,
        )
        unchecked_command_id = uuid4()
        unchecked_event = event.model_copy(update={
            "command_id": unchecked_command_id,
            "event_id": invitation_event_id(
                unchecked_command_id, created.world.ruleset_version, "attend", invitation.invitation_id,
            ),
            "work_calendar_checked": None,
        })
        unchecked_command = command.model_copy(update={
            "command_id": unchecked_command_id,
            "idempotency_key": "requested-npc-attendance-without-work-check",
        })
        with pytest.raises(InvalidEvent, match="must record its work-calendar check"):
            repository.commit_command(owner_scope, world_id, branch_id, unchecked_command, [unchecked_event])
        committed = repository.commit_command(owner_scope, world_id, branch_id, command, [event])
        assert committed.state_version == 4
        persisted = repository.read_world(owner_scope, world_id, branch_id).state.relationship_invitations[0]
        assert persisted == attended

        controlled_user = uuid4()
        repository.grant_membership(
            owner_scope,
            world_id,
            controlled_user,
            MembershipRole.PLAYER,
            controlled_actor_ids=[npc_id],
        )
        controlled_command_id = uuid4()
        controlled_event = event.model_copy(update={
            "command_id": controlled_command_id,
            "event_id": invitation_event_id(
                controlled_command_id, created.world.ruleset_version, "attend", invitation.invitation_id,
            ),
        })
        controlled_command = CommandEnvelope(
            command_id=controlled_command_id,
            actor_id=player_id,
            idempotency_key="requested-controlled-npc-attendance",
            expected_state_version=4,
            command_type=NPC_ATTENDANCE_COMMAND,
            payload=payload,
        )
        with pytest.raises(AccessDenied, match="user-controlled character"):
            repository.commit_command(owner_scope, world_id, branch_id, controlled_command, [controlled_event])

        player_scope = AccessScope(tenant_id=owner_scope.tenant_id, user_id=uuid4())
        repository.grant_membership(
            owner_scope,
            world_id,
            player_scope.user_id,
            MembershipRole.PLAYER,
            controlled_actor_ids=[player_id],
        )
        player_command_id = uuid4()
        player_event = event.model_copy(update={
            "command_id": player_command_id,
            "event_id": invitation_event_id(
                player_command_id, created.world.ruleset_version, "attend", invitation.invitation_id,
            ),
        })
        player_command = CommandEnvelope(
            command_id=player_command_id,
            actor_id=player_id,
            idempotency_key="player-requested-npc-attendance",
            expected_state_version=4,
            command_type=NPC_ATTENDANCE_COMMAND,
            payload=payload,
        )
        with pytest.raises(AccessDenied, match="world owner"):
            repository.commit_command(player_scope, world_id, branch_id, player_command, [player_event])
    finally:
        database.dispose()
