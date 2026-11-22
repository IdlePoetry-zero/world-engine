from __future__ import annotations

import io
from decimal import Decimal
from uuid import UUID, uuid4

import pytest

from world_engine.application.simulation_jobs import SimulationJobs
from world_engine.application.backups import BackupApplication
from world_engine.application.visibility import project_event
from world_engine.domain.errors import AccessDenied
from world_engine.domain.market_pricing import (
    PRICE_REVIEW_DAYS,
    PRICE_REVIEW_CONFIG_VERSION,
    PRICE_REVIEW_EVENT_TYPE,
    PRICE_REVIEW_HANDLER_ID,
    PRICE_REVIEW_HANDLER_VERSION,
    MarketListingPricePolicy,
    MarketPricingConfig,
)
from world_engine.domain.models import (
    AccessScope,
    NewWorld,
    ResourceAccount,
    ScheduledEventRecord,
    WorldEntity,
    WorldState,
)
from world_engine.simulation.contracts import WorldAdvancePlan
from world_engine.storage.database import Database
from world_engine.storage.backup_import import WorldBackupImporter
from world_engine.storage.repository import WorldRepository


def _configured_state():
    market_id, seller_id, listing_id, account_id, lot_id, schedule_id = (uuid4() for _ in range(6))
    day_seconds = 60
    review_period = PRICE_REVIEW_DAYS * day_seconds
    resource_id = "resource.wheat"
    offer = {
        "listing_id": listing_id,
        "resource_id": resource_id,
        "stock_account_id": account_id,
        "seller_id": seller_id,
        "unit_price_minor": 40,
        "minimum_quantity": "1",
        "active": True,
    }
    market = WorldEntity(
        id=market_id,
        kind="place",
        name="Market",
        components={
            "place": {"place_type": "settlement"},
            "market": {"revision": 1, "currency_resource_id": "core.coin", "offers": [offer]},
        },
    )
    seller = WorldEntity(
        id=seller_id,
        kind="character",
        name="Seller",
        status="active",
        location_id=market_id,
        components={"inventory": {
            "revision": 1,
            "lots": [{
                "lot_id": lot_id,
                "account_id": account_id,
                "resource_id": resource_id,
                "quantity": "4",
                "expiry_tick": None,
                "origin_event_id": None,
            }],
        }},
    )
    policy = MarketListingPricePolicy(
        policy_id="market.wheat",
        revision=1,
        reference_price_minor=100,
        target_stock_quantity=Decimal("10"),
        sensitivity_bps=10_000,
        max_markup_bps=5_000,
        max_discount_bps=5_000,
        market_id=market_id,
        listing_id=listing_id,
    )
    schedule = ScheduledEventRecord(
        id=schedule_id,
        due_tick=review_period,
        phase="maintenance",
        handler_id=PRICE_REVIEW_HANDLER_ID,
        handler_version=PRICE_REVIEW_HANDLER_VERSION,
        repeat_ticks=review_period,
        max_runs=3,
    )
    config = MarketPricingConfig(
        schema_version=PRICE_REVIEW_CONFIG_VERSION,
        review_period_days=PRICE_REVIEW_DAYS,
        policies=[policy],
    )
    state = WorldState.model_validate({
        "current_tick": 0,
        "calendar": {"day_seconds": day_seconds, "year_days": 360},
        "entities": {market_id: market, seller_id: seller},
        "resources": {
            account_id: ResourceAccount(resource_id=resource_id, balance=Decimal("4"), owner_id=seller_id),
        },
        "scheduled_events": [schedule],
        "metadata": {"market_pricing": config.model_dump(mode="json")},
    })
    return state, market_id, listing_id, schedule_id, review_period


def _run_world_advance(repository: WorldRepository, scope: AccessScope, world_id: UUID, branch_id: UUID, *, key: str, target_tick: int):
    jobs = SimulationJobs(repository)
    branch = repository.read_world(scope, world_id, branch_id).branch
    job = jobs.enqueue_world_advance(
        scope,
        world_id,
        branch_id,
        WorldAdvancePlan(started_tick=branch.current_tick, target_tick=target_tick),
        idempotency_key=key,
        speed="balanced",
        depth="deep",
        expected_state_version=branch.state_version,
    )
    lease = repository.claim_job(f"worker:{key}", job_type="simulation.world_advance")
    assert lease is not None
    for _ in range(8):
        jobs.advance(scope, lease)
        status = repository.read_job(scope, job.job_id)
        if status.status.value not in {"queued", "running"}:
            break
    assert repository.read_job(scope, job.job_id).status.value == "succeeded"
    return repository.read_job(scope, job.job_id)


def test_market_price_review_commits_at_due_tick_and_replays_independently_on_fork(tmp_path):
    state, market_id, listing_id, schedule_id, due_tick = _configured_state()
    scope = AccessScope(tenant_id=uuid4(), user_id=uuid4())
    outsider = AccessScope(tenant_id=scope.tenant_id, user_id=uuid4())
    database = Database(f"sqlite:///{(tmp_path / 'market-review.db').as_posix()}")
    database.create_schema()
    try:
        repository = WorldRepository(database)
        created = repository.create_world(scope, NewWorld(title="Market price review", state=state))
        world_id, source_branch_id = created.world.world_id, created.branch.branch_id

        fork = repository.fork(scope, world_id, source_branch_id, 0, "Independent review")
        fork_branch_id = fork.branch_id
        with pytest.raises(AccessDenied):
            repository.read_world(outsider, world_id, source_branch_id)

        _run_world_advance(
            repository, scope, world_id, source_branch_id, key="original-review", target_tick=due_tick,
        )
        original_head = repository.read_world(scope, world_id, source_branch_id)
        assert original_head.state.current_tick == due_tick
        assert original_head.state.entities[market_id].components["market"]["offers"][0]["unit_price_minor"] == 150
        original_schedule = next(item for item in original_head.state.scheduled_events if item.id == schedule_id)
        assert original_schedule.completed_runs == 1
        assert original_schedule.due_tick == due_tick * 2

        # The child starts from the exact same due schedule and period. Its worker
        # writes only to the fork; source branch state and head remain unchanged.
        source_state_hash = original_head.state_hash
        source_sequence = original_head.last_seq
        _run_world_advance(
            repository, scope, world_id, fork_branch_id, key="forked-review", target_tick=due_tick,
        )
        source_after_fork = repository.read_world(scope, world_id, source_branch_id)
        fork_head = repository.read_world(scope, world_id, fork_branch_id)
        assert source_after_fork.state_hash == source_state_hash
        assert source_after_fork.last_seq == source_sequence
        assert fork_head.state.current_tick == due_tick
        assert fork_head.state.entities[market_id].components["market"]["offers"][0]["unit_price_minor"] == 150

        expected_window_ticks = [14 * 60, 28 * 60, due_tick]
        original_events = repository.list_events(scope, world_id, source_branch_id).events
        fork_events = repository.list_events(scope, world_id, fork_branch_id).events
        original_clock_ticks = [
            item.payload["to_tick"] for item in original_events if item.type == "core.time_advanced.v1"
        ]
        fork_clock_ticks = [
            item.payload["to_tick"] for item in fork_events if item.type == "core.time_advanced.v1"
        ]
        assert original_clock_ticks == fork_clock_ticks == expected_window_ticks
        original_reviews = [item for item in original_events if item.type == PRICE_REVIEW_EVENT_TYPE]
        fork_reviews = [item for item in fork_events if item.type == PRICE_REVIEW_EVENT_TYPE]
        assert len(original_events) == len(fork_events) == 4
        assert len(original_reviews) == len(fork_reviews) == 1
        original_review = original_reviews[0]
        fork_review = fork_reviews[0]
        assert original_review.tick == fork_review.tick == due_tick
        assert original_review.payload["schedule_id"] == fork_review.payload["schedule_id"] == str(schedule_id)
        assert original_review.payload["period_index"] == fork_review.payload["period_index"] == 1
        assert original_review.event_id != fork_review.event_id
        assert original_review.command_id != fork_review.command_id
        assert original_review.payload["changes"][0]["listing_id"] == str(listing_id)

        assert repository.replay(scope, world_id, source_branch_id).state_hash == source_after_fork.state_hash
        assert repository.replay(scope, world_id, fork_branch_id).state_hash == fork_head.state_hash

        # Export and independently verify both committed branches, then import
        # the archive into an empty SQLite namespace without changing source IDs.
        source_hashes = {
            source_branch_id: source_after_fork.state_hash,
            fork_branch_id: fork_head.state_hash,
        }
        backup_app = BackupApplication(repository)
        archive = backup_app.export_world(scope, world_id)
        with backup_app.validate_archive_file_streaming(io.BytesIO(archive)) as verified:
            verified_summary = verified.summary()
            assert verified_summary["branch_count"] == 2
            assert verified_summary["event_count"] == 8
            for branch_id, digest in source_hashes.items():
                assert verified_summary["state_hashes"][str(branch_id)] == digest

            restored_db = Database(f"sqlite:///{(tmp_path / 'market-review-restored.db').as_posix()}")
            restored_db.create_schema()
            try:
                restored_repository = WorldRepository(restored_db)
                imported = WorldBackupImporter(restored_repository).import_verified_backup(scope, verified)
                assert imported.branch_count == 2
                assert imported.event_count == 8
                assert imported.state_hashes == source_hashes

                for branch_id, expected_hash in source_hashes.items():
                    restored_head = restored_repository.read_world(scope, world_id, branch_id)
                    assert restored_head.state_hash == expected_hash
                    assert restored_head.state.current_tick == due_tick
                    assert restored_head.state.entities[market_id].components["market"]["offers"][0][
                        "unit_price_minor"
                    ] == 150
                    restored_events = restored_repository.list_events(scope, world_id, branch_id).events
                    restored_clock_ticks = [
                        item.payload["to_tick"] for item in restored_events
                        if item.type == "core.time_advanced.v1"
                    ]
                    assert restored_clock_ticks == expected_window_ticks
                    restored_reviews = [
                        item for item in restored_events if item.type == PRICE_REVIEW_EVENT_TYPE
                    ]
                    assert len(restored_events) == 4
                    assert len(restored_reviews) == 1
                    restored_schedule = next(
                        item for item in restored_head.state.scheduled_events if item.id == schedule_id
                    )
                    assert restored_schedule.completed_runs == 1
                    assert restored_schedule.due_tick == due_tick * 2
                    assert restored_repository.replay(
                        scope, world_id, branch_id, from_genesis=True
                    ).state_hash == expected_hash

                    restored_review = restored_reviews[0]
                    assert restored_review.visibility == {"policy": "author_only"}
                    assert restored_review.tick == due_tick
                    assert restored_review.payload["schedule_id"] == str(schedule_id)
                    assert restored_review.payload["period_index"] == 1
                    assert restored_review.event_id == (
                        original_review.event_id if branch_id == source_branch_id else fork_review.event_id
                    )
                    # Public event projection must hide the audit even though the
                    # archived author can still inspect it in the event ledger.
                    assert project_event(
                        restored_review.model_dump(mode="json"), str(restored_head.state.player_id)
                    ) is None
            finally:
                restored_db.dispose()
    finally:
        database.dispose()
