"""SQLite integration coverage for authorized treasury reserve mobilization."""

from __future__ import annotations

from decimal import Decimal
from uuid import UUID, uuid4

import pytest

from world_engine.application.backups import BackupApplication
from world_engine.domain.components import GovernanceComponent, OfficeRecord, TreasuryComponent
from world_engine.domain.errors import InvalidEvent, StaleVersion
from world_engine.domain.models import (
    AccessScope,
    CommandEnvelope,
    MembershipRole,
    NewWorld,
    ResourceAccount,
    WorldEntity,
    WorldState,
)
from world_engine.rules import RuleContext, RuleEngine, parse_action
from world_engine.storage.backup_import import WorldBackupImporter
from world_engine.storage.database import Database
from world_engine.storage.repository import WorldRepository


CURRENCY_ID = "modern.credit"
OTHER_CURRENCY_ID = "modern.gold"
RULESET = "institutional-treasury-reserve-storage.v1"


def _organization(organization_id: UUID, signer_id: UUID, member_id: UUID, treasury_id: UUID) -> WorldEntity:
    governance = GovernanceComponent(
        members=[signer_id, member_id],
        leader_id=signer_id,
        offices=[OfficeRecord(
            office_id="office.leader",
            title="Treasury Representative",
            holder_id=signer_id,
            authority=["organization.lead", "authority.represent", "authority.treasury"],
        )],
        succession_policy="appointment",
    )
    return WorldEntity(
        id=organization_id,
        kind="organization",
        name=f"Institution {organization_id.hex[:6]}",
        components={
            "governance": governance.model_dump(mode="json"),
            "treasury": TreasuryComponent(accounts={CURRENCY_ID: treasury_id}).model_dump(mode="json"),
        },
    )


def _initial_state() -> tuple[WorldState, dict[str, UUID]]:
    organization_id = UUID(int=1)
    signer_id, member_id = UUID(int=2), UUID(int=3)
    foreign_organization_id = UUID(int=4)
    foreign_signer_id, foreign_member_id = UUID(int=5), UUID(int=6)
    treasury_id, reserve_a_id, reserve_b_id = UUID(int=10), UUID(int=11), UUID(int=12)
    foreign_treasury_id, foreign_reserve_id = UUID(int=13), UUID(int=14)
    other_currency_reserve_id = UUID(int=15)

    entities = {
        signer_id: WorldEntity(id=signer_id, kind="character", name="Treasury signer"),
        member_id: WorldEntity(id=member_id, kind="character", name="Ordinary member"),
        foreign_signer_id: WorldEntity(id=foreign_signer_id, kind="character", name="Other signer"),
        foreign_member_id: WorldEntity(id=foreign_member_id, kind="character", name="Other member"),
        organization_id: _organization(organization_id, signer_id, member_id, treasury_id),
        foreign_organization_id: _organization(
            foreign_organization_id, foreign_signer_id, foreign_member_id, foreign_treasury_id,
        ),
    }
    state = WorldState(
        current_tick=17,
        player_id=signer_id,
        entities=entities,
        resources={
            treasury_id: ResourceAccount(resource_id=CURRENCY_ID, balance="5", owner_id=organization_id),
            # UUID order makes the canonical allocation order explicit. The first
            # reserve contributes all six units and the second contributes four.
            reserve_a_id: ResourceAccount(resource_id=CURRENCY_ID, balance="6", owner_id=organization_id),
            reserve_b_id: ResourceAccount(resource_id=CURRENCY_ID, balance="8", owner_id=organization_id),
            foreign_treasury_id: ResourceAccount(
                resource_id=CURRENCY_ID, balance="70", owner_id=foreign_organization_id,
            ),
            foreign_reserve_id: ResourceAccount(
                resource_id=CURRENCY_ID, balance="50", owner_id=foreign_organization_id,
            ),
            other_currency_reserve_id: ResourceAccount(
                resource_id=OTHER_CURRENCY_ID, balance="25", owner_id=organization_id,
            ),
        },
    )
    return state, {
        "organization": organization_id,
        "signer": signer_id,
        "member": member_id,
        "treasury": treasury_id,
        "reserve_a": reserve_a_id,
        "reserve_b": reserve_b_id,
        "foreign_organization": foreign_organization_id,
        "foreign_signer": foreign_signer_id,
        "foreign_treasury": foreign_treasury_id,
        "foreign_reserve": foreign_reserve_id,
        "other_currency_reserve": other_currency_reserve_id,
    }


def _resolve(repository, scope, world_id, branch_id, actor_id, command_id, payload):
    view = repository.read_world(scope, world_id, branch_id)
    action = parse_action(payload)
    resolution = RuleEngine().evaluate(
        view.state,
        action,
        RuleContext(
            world_id=world_id,
            branch_id=branch_id,
            command_id=command_id,
            actor_id=actor_id,
            ruleset_version=view.world.ruleset_version,
        ),
    )
    return view, action.model_dump(mode="json"), resolution


def _command(*, actor_id, command_id, expected_state_version, normalized, idempotency_key):
    return CommandEnvelope(
        command_id=command_id,
        actor_id=actor_id,
        idempotency_key=idempotency_key,
        expected_state_version=expected_state_version,
        command_type=f"action.{normalized['kind']}",
        payload=normalized,
    )


def test_treasury_reserve_mobilization_commits_exact_signed_sweep_and_replays(tmp_path):
    database = Database(f"sqlite:///{(tmp_path / 'institutional-treasury-reserve.sqlite').as_posix()}")
    database.create_schema()
    repository = WorldRepository(database)
    owner_scope = AccessScope(tenant_id=uuid4(), user_id=uuid4())
    initial, ids = _initial_state()
    created = repository.create_world(
        owner_scope,
        NewWorld(title="Treasury reserve mobilization", ruleset_version=RULESET, state=initial),
    )
    world_id, branch_id = created.world.world_id, created.branch.branch_id

    action_payload = {
        "kind": "mobilize_treasury_reserve",
        "organization_id": str(ids["organization"]),
        "currency_resource_id": CURRENCY_ID,
        "amount_minor": 10,
    }
    try:
        # A player may commit only as a controlled character. Giving this test
        # member control also lets the repository prove that signer authority is
        # independently enforced by the rule and event validators.
        player_id = uuid4()
        repository.grant_membership(
            owner_scope, world_id, player_id, MembershipRole.PLAYER,
            controlled_actor_ids=[ids["signer"], ids["member"]],
        )
        player_scope = AccessScope(tenant_id=owner_scope.tenant_id, user_id=player_id)

        initial_view = repository.read_world(player_scope, world_id, branch_id)
        before_hash = initial_view.state_hash
        before_seq = initial_view.last_seq
        before_tick = initial_view.state.current_tick

        command_id = uuid4()
        view, normalized, resolution = _resolve(
            repository, player_scope, world_id, branch_id, ids["signer"], command_id, action_payload,
        )
        assert resolution.outcome.status == "succeeded", resolution.outcome.model_dump(mode="json")
        (event,) = resolution.events
        assert event.type == "core.institutional_treasury_mobilized.v1"
        assert event.world_id == world_id
        assert event.branch_id == branch_id
        assert event.actor_id == ids["signer"]
        assert event.command_id == command_id
        assert event.ruleset_version == RULESET
        assert event.organization_id == ids["organization"]
        assert event.currency_resource_id == CURRENCY_ID
        assert event.amount_minor == 10
        assert event.destination_account_id == ids["treasury"]
        assert [(leg.account_id, leg.quantity) for leg in event.allocations] == [
            (ids["reserve_a"], Decimal("6")),
            (ids["reserve_b"], Decimal("4")),
        ]
        command = _command(
            actor_id=ids["signer"], command_id=command_id,
            expected_state_version=view.branch.state_version, normalized=normalized,
            idempotency_key="mobilize-owned-reserves-once",
        )

        # A member without treasury authority cannot make this transfer, even
        # when that character is controlled by the committing player.
        unauthorized_view, _, unauthorized = _resolve(
            repository, player_scope, world_id, branch_id, ids["member"], uuid4(), action_payload,
        )
        assert unauthorized.outcome.status == "rejected"
        assert unauthorized.outcome.code == "treasury_reserve_signer_unauthorized"
        assert unauthorized.events == []
        unchanged_after_rejection = repository.read_world(player_scope, world_id, branch_id)
        assert unchanged_after_rejection.state_hash == before_hash
        assert unchanged_after_rejection.last_seq == before_seq
        assert unauthorized_view.state_hash == before_hash

        # A forged event and matching command actor still fail at the commit
        # boundary. This prevents world-owner or fixture setup from bypassing
        # the organization's current signer check.
        forged_event = event.model_copy(update={"actor_id": ids["member"]})
        forged_command = _command(
            actor_id=ids["member"], command_id=command_id,
            expected_state_version=view.branch.state_version, normalized=normalized,
            idempotency_key="forged-treasury-signer",
        )
        with pytest.raises(InvalidEvent):
            repository.commit_command(owner_scope, world_id, branch_id, forged_command, [forged_event])
        unchanged_after_forgery = repository.read_world(owner_scope, world_id, branch_id)
        assert unchanged_after_forgery.state_hash == before_hash
        assert unchanged_after_forgery.last_seq == before_seq

        # Even a valid signer cannot redirect the canonical sweep to a foreign
        # account by submitting a syntactically balanced event allocation.
        diverted_leg = event.allocations[0].model_copy(update={
            "account_id": ids["foreign_reserve"], "quantity": Decimal("10"),
        })
        diverted_event = event.model_copy(update={"allocations": (diverted_leg,)})
        diverted_command = _command(
            actor_id=ids["signer"], command_id=command_id,
            expected_state_version=view.branch.state_version, normalized=normalized,
            idempotency_key="divert-treasury-reserve-allocation",
        )
        with pytest.raises(InvalidEvent):
            repository.commit_command(owner_scope, world_id, branch_id, diverted_command, [diverted_event])
        unchanged_after_diversion = repository.read_world(owner_scope, world_id, branch_id)
        assert unchanged_after_diversion.state_hash == before_hash
        assert unchanged_after_diversion.last_seq == before_seq

        competing_id = uuid4()
        competing_view, competing_normalized, competing_resolution = _resolve(
            repository, player_scope, world_id, branch_id, ids["signer"], competing_id, action_payload,
        )
        assert competing_view.branch.state_version == view.branch.state_version
        assert competing_resolution.outcome.status == "succeeded"
        (competing_event,) = competing_resolution.events
        competing_command = _command(
            actor_id=ids["signer"], command_id=competing_id,
            expected_state_version=competing_view.branch.state_version, normalized=competing_normalized,
            idempotency_key="mobilize-stale-competitor",
        )

        committed = repository.commit_command(player_scope, world_id, branch_id, command, [event])
        after = repository.read_world(player_scope, world_id, branch_id)
        assert after.state.current_tick == before_tick == 17
        assert after.branch.state_version == view.branch.state_version + 1
        assert after.last_seq == before_seq + 1
        assert after.state.resources[ids["treasury"]].balance == Decimal("15")
        assert after.state.resources[ids["reserve_a"]].balance == Decimal("0")
        assert after.state.resources[ids["reserve_b"]].balance == Decimal("4")
        assert after.state.resources[ids["foreign_treasury"]].balance == Decimal("70")
        assert after.state.resources[ids["foreign_reserve"]].balance == Decimal("50")
        assert after.state.resources[ids["other_currency_reserve"]].balance == Decimal("25")

        with pytest.raises(StaleVersion):
            repository.commit_command(player_scope, world_id, branch_id, competing_command, [competing_event])
        after_stale = repository.read_world(player_scope, world_id, branch_id)
        assert after_stale.state_hash == after.state_hash
        assert after_stale.last_seq == after.last_seq
        assert after_stale.state.resources[ids["treasury"]].balance == Decimal("15")

        duplicate = repository.commit_command(player_scope, world_id, branch_id, command, [event])
        assert duplicate.duplicate is True
        after_duplicate = repository.read_world(player_scope, world_id, branch_id)
        assert after_duplicate.state_hash == after.state_hash
        assert after_duplicate.last_seq == after.last_seq
        assert after_duplicate.state.resources[ids["treasury"]].balance == Decimal("15")

        page = repository.list_events(owner_scope, world_id, branch_id, after_seq=before_seq, limit=10)
        (persisted_record,) = page.events
        assert persisted_record.type == "core.institutional_treasury_mobilized.v1"
        assert persisted_record.tick == before_tick
        assert persisted_record.command_id == command_id

        # The ledger persists the exact debit split and balancing treasury
        # credit from the typed event, with no unrelated account movement.
        with database.engine.connect() as connection:
            rows = connection.exec_driver_sql(
                "SELECT account_id, resource_id, delta FROM ledger_postings WHERE event_id = ? ORDER BY account_id",
                (event.event_id.hex,),
            ).fetchall()
        persisted_postings = {
            (UUID(hex=row[0]), row[1]): Decimal(row[2])
            for row in rows
        }
        assert persisted_postings == {
            (ids["reserve_a"], CURRENCY_ID): Decimal("-6"),
            (ids["reserve_b"], CURRENCY_ID): Decimal("-4"),
            (ids["treasury"], CURRENCY_ID): Decimal("10"),
        }

        replayed = repository.replay(owner_scope, world_id, branch_id, from_genesis=True)
        assert replayed.state_hash == after.state_hash
        assert replayed.state.resources == after.state.resources
        assert replayed.state.current_tick == before_tick

        backup = BackupApplication(repository)
        archive = backup.export_world(owner_scope, world_id)
        restored_database = Database(f"sqlite:///{(tmp_path / 'institutional-treasury-restored.sqlite').as_posix()}")
        restored_database.create_schema()
        restored_repository = WorldRepository(restored_database)
        try:
            with backup.validate_archive(archive) as verified:
                imported = WorldBackupImporter(restored_repository).import_verified_backup(owner_scope, verified)
            assert imported.world_id == world_id
            restored = restored_repository.read_world(owner_scope, world_id, branch_id)
            assert restored.state_hash == after.state_hash
            assert restored.state.resources == after.state.resources
            assert restored_repository.replay(
                owner_scope, world_id, branch_id, from_genesis=True,
            ).state_hash == after.state_hash
        finally:
            restored_database.dispose()
    finally:
        database.dispose()
