"""Integration checks for hosted voting, control permissions, and receipt recovery."""

from types import SimpleNamespace
from uuid import uuid4

import pytest

from world_engine.adapters.models import RoleTask
from world_engine.api.contracts import MessageRequest
from world_engine.application.actions import RulesActionDispatcher
from world_engine.application.backups import BackupApplication
from world_engine.application.conversation import ConversationApplication
from world_engine.application.visibility import project_player_state
from world_engine.domain.errors import AccessDenied
from world_engine.domain.models import AccessScope, CommandEnvelope, MembershipRole, NewWorld, WorldEntity, WorldState
from world_engine.rules import ACTION_INTENT_ADAPTER, RuleContext, RuleEngine
from world_engine.storage.database import Database
from world_engine.storage.backup_import import WorldBackupImporter
from world_engine.storage.repository import WorldRepository


@pytest.mark.asyncio
@pytest.mark.parametrize("choice", ["yes", "defer", "controlled"])
async def test_hosted_vote_is_attributed_and_private(tmp_path, choice):
    owner = AccessScope(tenant_id=uuid4(), user_id=uuid4())
    player, nominee, voter, org = (uuid4() for _ in range(4))
    people = {
        person_id: WorldEntity(id=person_id, kind="character", name=name)
        for person_id, name in ((player, "Incumbent"), (nominee, "Nominee"), (voter, "Voter"))
    }
    organization = WorldEntity(
        id=org, kind="organization", name="Assembly",
        components={"governance": {"leader_id": str(player), "members": [str(person) for person in people],
                     "offices": [{"office_id": "office.leader", "title": "Leader", "holder_id": str(player)}],
                     "succession_policy": "election", "charter": "Appoint a successor by ballot."}},
    )
    db = Database(f"sqlite:///{(tmp_path / 'ballot.sqlite').as_posix()}")
    db.create_schema()
    try:
        repo = WorldRepository(db)
        created = repo.create_world(owner, NewWorld(
            title="Hosted ballot", ruleset_version="hosted-ballot.v1",
            state=WorldState(player_id=player, entities={**people, org: organization}),
        ))
        wid, bid = created.world.world_id, created.branch.branch_id
        engine = RuleEngine()
        initial = repo.read_world(owner, wid, bid)
        command_id = uuid4()
        payload = {"kind": "open_leadership_ballot", "organization_id": str(org), "nominee_id": str(nominee)}
        result = engine.evaluate(initial.state, payload, RuleContext(
            world_id=wid, branch_id=bid, command_id=command_id, actor_id=player,
            ruleset_version=initial.world.ruleset_version,
        ))
        assert result.outcome.status == "succeeded"
        repo.commit_command(owner, wid, bid, CommandEnvelope(
            command_id=command_id, actor_id=player, expected_state_version=initial.branch.state_version,
            idempotency_key="proposal", command_type="action.open_leadership_ballot", payload=payload,
        ), result.events)
        head = repo.read_world(owner, wid, bid)
        ballot, = head.state.leadership_ballots
        proposal = {"kind": "request_leadership_ballot_vote", "ballot_id": str(ballot.ballot_id),
                    "voter_id": str(voter), "expected_ballot_revision": 1}
        calls = []
        class Models:
            async def generate(self, scope, request):
                calls.append(request.role_task)
                output = ({"action": proposal, "clarification": ""} if request.role_task is RoleTask.INTENT
                          else {"choice": choice})
                return SimpleNamespace(normalized_output=output)
        request = MessageRequest(request_id=uuid4(), world_id=wid, branch_id=bid, actor_id=player,
            session_id=uuid4(), expected_state_version=str(head.branch.state_version), mode="action",
            text="请该合资格角色就现有提名独立投票。")
        dispatcher = RulesActionDispatcher(repo, Models(), engine, ACTION_INTENT_ADAPTER, RuleContext)
        if choice == "controlled":
            other = AccessScope(tenant_id=owner.tenant_id, user_id=uuid4())
            repo.grant_membership(owner, wid, other.user_id, MembershipRole.PLAYER, controlled_actor_ids=[voter])
            with pytest.raises(AccessDenied):
                await dispatcher.execute(owner, head, request, "action")
            assert calls == [RoleTask.INTENT]
            return
        result = await dispatcher.execute(owner, head, request, "action")
        after = repo.read_world(owner, wid, bid)
        assert calls == [RoleTask.INTENT, RoleTask.NPC_DECISION]
        if choice == "defer":
            assert result["action_status"] == "partial" and after.state_hash == head.state_hash
            return
        assert result["content"] == "该角色已投票。"
        stored = repo.read_command_receipt(owner, wid, bid, request.request_id, player, f"message:{request.request_id}")
        event, = stored.events
        assert event.actor_id == player and event.payload["requested_by"] == str(player)
        assert event.payload["actor_id"] == str(voter)
        assert event.payload["resulting_ballot"]["votes"] == [{"voter_id": str(voter), "choice": "yes"}]
        assert repo.replay(owner, wid, bid, from_genesis=True).state_hash == after.state_hash
        owner_ballot, = project_player_state(after.state.model_dump(mode="json"), str(player))["own_leadership_ballots"]
        assert "votes" not in owner_ballot and owner_ballot["own_vote"] is None
        conversation = ConversationApplication(SimpleNamespace(repository=repo), Models(), None)
        recovered = conversation._committed_status(owner, wid, bid, request.request_id, player)
        assert recovered["result"]["content"] == result["content"]
        backup = BackupApplication(repo)
        restored_db = Database(f"sqlite:///{(tmp_path / 'restored.sqlite').as_posix()}")
        restored_db.create_schema()
        try:
            restored = WorldRepository(restored_db)
            with backup.validate_archive(backup.export_world(owner, wid)) as archive:
                WorldBackupImporter(restored).import_verified_backup(owner, archive)
            assert restored.replay(owner, wid, bid, from_genesis=True).state_hash == after.state_hash
        finally:
            restored_db.dispose()
    finally:
        db.dispose()
