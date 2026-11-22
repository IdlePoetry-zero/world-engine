from uuid import uuid4

from sqlalchemy import select

import world_engine.storage.repository as repository_module
from world_engine.domain.models import (
    AccessScope,
    CommandEnvelope,
    NewWorld,
    ResourceAccount,
    ResourceTransferredEvent,
    TimeAdvancedEvent,
    WorldEntity,
    WorldState,
)
from world_engine.storage.database import Database
from world_engine.storage.models import LedgerPostingRow, OutboxRow
from world_engine.storage.repository import WorldRepository


def test_commit_reuses_prevalidated_per_event_postings(monkeypatch):
    database = Database("sqlite:///:memory:")
    database.create_schema()
    repository = WorldRepository(database)
    try:
        tenant_id, owner_id, actor_id, recipient_id = (uuid4() for _ in range(4))
        source_account_id, target_account_id = uuid4(), uuid4()
        scope = AccessScope(tenant_id=tenant_id, user_id=owner_id)
        created = repository.create_world(
            scope,
            NewWorld(
                title="Ledger posting reuse",
                state=WorldState(
                    player_id=actor_id,
                    entities={
                        actor_id: WorldEntity(id=actor_id, kind="character", name="Mira"),
                        recipient_id: WorldEntity(id=recipient_id, kind="character", name="Sora"),
                    },
                    resources={
                        source_account_id: ResourceAccount(
                            resource_id="core.silver", balance="10", owner_id=actor_id,
                        ),
                        target_account_id: ResourceAccount(
                            resource_id="core.silver", balance="0", owner_id=recipient_id,
                        ),
                    },
                ),
            ),
        )
        transfer = ResourceTransferredEvent(
            from_account_id=source_account_id,
            to_account_id=target_account_id,
            resource_id="core.silver",
            quantity="2.5",
        )
        clock = TimeAdvancedEvent(to_tick=30)
        command = CommandEnvelope(
            actor_id=actor_id,
            idempotency_key="posting-reuse-once",
            expected_state_version=0,
            command_type="core.transfer",
            payload={"quantity": "2.5"},
        )

        calls = []
        original_ledger_postings = repository_module.ledger_postings

        def counted_ledger_postings(event):
            calls.append(event.event_id)
            return original_ledger_postings(event)

        monkeypatch.setattr(repository_module, "ledger_postings", counted_ledger_postings)
        result = repository.commit_command(
            scope,
            created.world.world_id,
            created.branch.branch_id,
            command,
            [transfer, clock],
        )

        assert result.state_version == 1
        assert (result.first_seq, result.last_seq) == (1, 2)
        assert calls == [transfer.event_id, clock.event_id]

        with repository._transaction() as session:
            postings = session.scalars(
                select(LedgerPostingRow)
                .where(LedgerPostingRow.event_id == transfer.event_id)
                .order_by(LedgerPostingRow.posting_index)
            ).all()
            outbox = session.scalars(
                select(OutboxRow).where(OutboxRow.branch_id == created.branch.branch_id)
            ).all()

        assert [
            (row.posting_index, row.account_id, row.resource_id, row.delta)
            for row in postings
        ] == [
            (0, source_account_id, "core.silver", "-2.5"),
            (1, target_account_id, "core.silver", "2.5"),
        ]
        assert {row.event_id for row in outbox} == {transfer.event_id, clock.event_id}
    finally:
        database.dispose()
