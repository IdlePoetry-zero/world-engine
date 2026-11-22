"""Storage contract for author cancellation of invalidated ballots."""

from decimal import Decimal
from uuid import uuid4

import pytest

from world_engine.domain.components import GovernanceComponent, OfficeRecord, ProvenanceReference
from world_engine.domain.errors import AccessDenied, InvalidEvent, StaleVersion
from world_engine.domain.gameplay_events import AuthorComponentRevisedEvent
from world_engine.domain.leadership_ballot_events import LeadershipBallotChangedEvent
from world_engine.domain.leadership_ballot_state import BALLOT_COMMANDS
from world_engine.domain.models import (
    AccessScope,
    CommandEnvelope,
    MembershipRole,
    NewWorld,
    WorldEntity,
    WorldState,
)
from world_engine.domain.reducers import author_value_hash
from world_engine.rules import RuleContext, RuleEngine
from world_engine.storage.database import Database
from world_engine.storage.repository import WorldRepository


def _fixture_state():
    incumbent_id, nominee_id, voter_a_id, voter_b_id, organization_id = (uuid4() for _ in range(5))
    members = [incumbent_id, nominee_id, voter_a_id, voter_b_id]
    governance = GovernanceComponent(
        revision=6,
        members=members,
        leader_id=incumbent_id,
        offices=[OfficeRecord(
            office_id="office.leader",
            title="Leader",
            holder_id=incumbent_id,
            authority=["organization.lead"],
        )],
        succession_policy="election",
        decision_threshold=Decimal("0.5"),
    )
    entities = {
        member_id: WorldEntity(id=member_id, kind="character", name=name, status="active")
        for member_id, name in zip(
            members, ("Incumbent", "Nominee", "Voter A", "Voter B"), strict=True,
        )
    }
    entities[organization_id] = WorldEntity(
        id=organization_id,
        kind="organization",
        name="Harbor Council",
        status="active",
        components={"governance": governance.model_dump(mode="json")},
    )
    return (
        WorldState(player_id=incumbent_id, entities=entities),
        incumbent_id,
        nominee_id,
        voter_a_id,
        voter_b_id,
        organization_id,
    )


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


def _author_bump_governance(repository, scope, world_id, branch_id, organization_id):
    view = repository.read_world(scope, world_id, branch_id)
    organization = view.state.entities[organization_id]
    old_governance = GovernanceComponent.model_validate(organization.components["governance"])
    replacement = old_governance.model_copy(update={
        "revision": old_governance.revision + 1,
        "decision_threshold": Decimal("0.6"),
    })
    event = AuthorComponentRevisedEvent(
        entity_id=organization_id,
        author_id=scope.user_id,
        component_name="governance",
        expected_previous_hash=author_value_hash(
            organization.components["governance"], component_name="governance",
        ),
        replacement=replacement.model_dump(mode="json"),
        revision_note="Update the council's decision threshold.",
        provenance=[ProvenanceReference(kind="pack", reference_id="ballot-cancellation-test")],
    )
    command = CommandEnvelope(
        command_id=uuid4(),
        actor_id=scope.user_id,
        idempotency_key="revise-governance-for-ballot-test",
        expected_state_version=view.branch.state_version,
        command_type="author.revise_component",
    )
    return repository.commit_command(scope, world_id, branch_id, command, [event])


def test_invalidated_ballot_cancel_is_scoped_authorized_and_replayable(tmp_path):
    database = Database(f"sqlite:///{(tmp_path / 'invalidated-ballot.sqlite').as_posix()}")
    database.create_schema()
    repository = WorldRepository(database)
    owner_scope = AccessScope(tenant_id=uuid4(), user_id=uuid4())
    try:
        state, incumbent_id, nominee_id, voter_a_id, _voter_b_id, organization_id = _fixture_state()
        created = repository.create_world(
            owner_scope,
            NewWorld(title="Invalidated ballot maintenance", state=state),
        )
        world_id = created.world.world_id
        branch_id = created.branch.branch_id

        opened, open_event = _commit_action(
            repository,
            owner_scope,
            world_id,
            branch_id,
            incumbent_id,
            {"kind": "open_leadership_ballot", "organization_id": str(organization_id),
             "nominee_id": str(nominee_id)},
            idempotency_key="open-for-invalidated-cancel",
        )
        ballot_id = open_event.resulting_ballot.ballot_id
        _, vote_event = _commit_action(
            repository,
            owner_scope,
            world_id,
            branch_id,
            voter_a_id,
            {"kind": "cast_leadership_ballot_vote", "ballot_id": str(ballot_id),
             "expected_ballot_revision": 1, "choice": "yes"},
            idempotency_key="vote-before-invalidation",
        )
        voted_ballot = vote_event.resulting_ballot
        assert voted_ballot.status == "open"
        assert len(voted_ballot.votes) == 1

        source_before_author = repository.read_world(owner_scope, world_id, branch_id)
        # A maintenance cancellation must not be available while the pinned
        # governance snapshot remains valid.
        with pytest.raises(InvalidEvent, match="still valid"):
            repository.cancel_invalidated_ballot(
                owner_scope,
                world_id,
                branch_id,
                ballot_id,
                command_id=uuid4(),
                expected_state_version=source_before_author.branch.state_version,
                expected_ballot_revision=voted_ballot.revision,
            )

        fork = repository.fork(
            owner_scope,
            world_id,
            branch_id,
            source_before_author.last_seq,
            "Healthy ballot before governance edit",
        )
        child_before_author = repository.read_world(owner_scope, world_id, fork.branch_id)
        assert child_before_author.state.leadership_ballots[0] == voted_ballot

        _author_bump_governance(repository, owner_scope, world_id, branch_id, organization_id)
        invalidated = repository.read_world(owner_scope, world_id, branch_id)
        invalidated_ballot = invalidated.state.leadership_ballots[0]
        assert invalidated_ballot.status == "open"
        assert invalidated_ballot.votes == voted_ballot.votes
        assert invalidated.state.entities[organization_id].components["governance"]["revision"] == (
            voted_ballot.governance_revision + 1
        )

        # The fork still has the old governance revision and remains healthy.
        with pytest.raises(InvalidEvent, match="still valid"):
            repository.cancel_invalidated_ballot(
                owner_scope,
                world_id,
                fork.branch_id,
                ballot_id,
                command_id=uuid4(),
                expected_state_version=child_before_author.branch.state_version,
                expected_ballot_revision=voted_ballot.revision,
            )

        # Both compare-and-swap dimensions are checked before cancellation.
        with pytest.raises(StaleVersion):
            repository.cancel_invalidated_ballot(
                owner_scope,
                world_id,
                branch_id,
                ballot_id,
                command_id=uuid4(),
                expected_state_version=invalidated.branch.state_version - 1,
                expected_ballot_revision=invalidated_ballot.revision,
            )
        with pytest.raises(StaleVersion):
            repository.cancel_invalidated_ballot(
                owner_scope,
                world_id,
                branch_id,
                ballot_id,
                command_id=uuid4(),
                expected_state_version=invalidated.branch.state_version,
                expected_ballot_revision=invalidated_ballot.revision - 1,
            )

        player_user_id = uuid4()
        player_scope = AccessScope(tenant_id=owner_scope.tenant_id, user_id=player_user_id)
        repository.grant_membership(
            owner_scope,
            world_id,
            player_user_id,
            MembershipRole.PLAYER,
            controlled_actor_ids=[voter_a_id],
        )
        with pytest.raises(AccessDenied):
            repository.cancel_invalidated_ballot(
                player_scope,
                world_id,
                branch_id,
                ballot_id,
                command_id=uuid4(),
                expected_state_version=invalidated.branch.state_version,
                expected_ballot_revision=invalidated_ballot.revision,
            )

        cancel_command_id = uuid4()
        cancel_result = repository.cancel_invalidated_ballot(
            owner_scope,
            world_id,
            branch_id,
            ballot_id,
            command_id=cancel_command_id,
            expected_state_version=invalidated.branch.state_version,
            expected_ballot_revision=invalidated_ballot.revision,
        )
        assert cancel_result.duplicate is False
        cancelled = repository.read_world(owner_scope, world_id, branch_id)
        cancelled_ballot = cancelled.state.leadership_ballots[0]
        assert cancelled_ballot.status == "cancelled"
        assert cancelled_ballot.votes == voted_ballot.votes
        assert cancelled_ballot.revision == invalidated_ballot.revision + 1
        assert cancelled.state.current_tick == invalidated.state.current_tick
        assert cancelled_ballot.updated_tick == invalidated_ballot.updated_tick

        retry = repository.cancel_invalidated_ballot(
            owner_scope,
            world_id,
            branch_id,
            ballot_id,
            command_id=cancel_command_id,
            expected_state_version=invalidated.branch.state_version,
            expected_ballot_revision=invalidated_ballot.revision,
        )
        assert retry.duplicate is True
        assert retry.command_id == cancel_result.command_id
        assert retry.first_seq == cancel_result.first_seq
        assert retry.last_seq == cancel_result.last_seq
        assert retry.state_hash == cancel_result.state_hash

        source_head = repository.read_world(owner_scope, world_id, branch_id)
        child_head = repository.read_world(owner_scope, world_id, fork.branch_id)
        source_replay = repository.replay(owner_scope, world_id, branch_id, from_genesis=True)
        child_replay = repository.replay(owner_scope, world_id, fork.branch_id, from_genesis=True)
        assert source_replay.state_hash == source_head.state_hash
        assert child_replay.state_hash == child_head.state_hash
        assert source_replay.state.leadership_ballots[0].status == "cancelled"
        assert source_replay.state.leadership_ballots[0].votes == voted_ballot.votes
        assert child_replay.state.leadership_ballots[0] == voted_ballot
        assert child_replay.state.entities[organization_id].components["governance"]["revision"] == (
            voted_ballot.governance_revision
        )

        _, new_event = _commit_action(
            repository, owner_scope, world_id, branch_id, incumbent_id,
            {"kind": "open_leadership_ballot", "organization_id": str(organization_id), "nominee_id": str(nominee_id)},
            idempotency_key="open-after-invalidated-cancellation",
        )
        assert new_event.resulting_ballot.threshold == Decimal("0.6")
        assert new_event.resulting_ballot.ballot_id != ballot_id and not new_event.resulting_ballot.votes
        assert repository.read_world(owner_scope, world_id, branch_id).state.leadership_ballots[0] == cancelled_ballot
    finally:
        database.dispose()
