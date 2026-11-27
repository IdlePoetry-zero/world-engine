"""Benchmark storage read, commit, checkpoint, and replay paths on a scratch DB.

Run from the repository root with the project runtime:
    ./.runtime/venv313/Scripts/python.exe scripts/benchmark-causal-storage.py --phase baseline
    ./.runtime/venv313/Scripts/python.exe scripts/benchmark-causal-storage.py --phase after

Every run builds the same small deterministic world in a temporary SQLite file.
It never opens the application's configured database or reads live-world data.
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import tempfile
from pathlib import Path
from time import perf_counter_ns
from uuid import UUID, uuid5


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from world_engine.domain.models import (  # noqa: E402
    AccessScope,
    CommandEnvelope,
    TimeAdvancedEvent,
)
from world_engine.domain.reducers import state_hash, state_json, state_payload_hash  # noqa: E402
from world_engine.content.catalog import ContentCatalog  # noqa: E402
from world_engine.storage.database import Database  # noqa: E402
from world_engine.storage.repository import WorldRepository  # noqa: E402
from world_engine.storage.validated_state_cache import ValidatedBranchStateCache  # noqa: E402


_FIXTURE_NAMESPACE = UUID("74b5a09e-082c-4c0c-9f76-6767928b62db")


def _median_ms(samples_ns: list[int]) -> float:
    return round(statistics.median(samples_ns) / 1_000_000, 3)


def _timed(callable_, samples: int) -> dict[str, object]:
    elapsed = []
    for _ in range(samples):
        start = perf_counter_ns()
        callable_()
        elapsed.append(perf_counter_ns() - start)
    return {
        "samples": len(elapsed),
        "median_ms": _median_ms(elapsed),
        "min_ms": round(min(elapsed) / 1_000_000, 3),
        "max_ms": round(max(elapsed) / 1_000_000, 3),
    }


def _snapshot_payload_encoding_comparison(state, samples: int) -> dict[str, object]:
    """Isolate the avoidable second canonical model dump from database work."""
    baseline_times = []
    reuse_times = []
    baseline_result = None
    reuse_result = None
    for index in range(samples):
        operations = (
            ((baseline_times, "baseline"), (reuse_times, "reuse"))
            if index % 2 == 0 else ((reuse_times, "reuse"), (baseline_times, "baseline"))
        )
        for (timings, variant) in operations:
            start = perf_counter_ns()
            if variant == "baseline":
                digest = state_hash(state)
                payload = state_json(state)
                baseline_result = (digest, payload)
            else:
                payload = state_json(state)
                digest = state_payload_hash(payload)
                reuse_result = (digest, payload)
            timings.append(perf_counter_ns() - start)
    if baseline_result is None or reuse_result is None or baseline_result != reuse_result:
        raise RuntimeError("Snapshot payload reuse changed the canonical payload or digest.")
    return {
        "baseline_duplicate_canonical_dump": {
            "samples": len(baseline_times), "median_ms": _median_ms(baseline_times),
        },
        "single_payload_reuse": {
            "samples": len(reuse_times), "median_ms": _median_ms(reuse_times),
        },
    }


def run(phase: str, samples: int) -> dict[str, object]:
    tenant_id = uuid5(_FIXTURE_NAMESPACE, "tenant")
    user_id = uuid5(_FIXTURE_NAMESPACE, "owner")
    world_draft = ContentCatalog().build_world(
        "modern_city", "Causal storage benchmark", "Storage benchmark", seed=42,
    )
    initial = world_draft.state
    world_id, branch_id = world_draft.world_id, world_draft.branch_id
    actor_id = initial.player_id
    if actor_id is None:
        raise RuntimeError("The deterministic catalog fixture has no player character.")
    initial_hash = state_hash(initial)
    serialization_comparison = _snapshot_payload_encoding_comparison(initial, max(3, min(samples, 6)))
    initial_payload = state_json(initial)
    initial_bytes = len(json.dumps(
        initial_payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8"))
    scope = AccessScope(tenant_id=tenant_id, user_id=user_id)

    with tempfile.TemporaryDirectory(prefix="world-engine-storage-benchmark-") as temporary:
        database_path = Path(temporary) / "scratch.sqlite"
        database = Database(f"sqlite:///{database_path.as_posix()}")
        try:
            database.create_schema()
            repository = WorldRepository(database)
            created = repository.create_world(scope, world_draft)

            def cold_read() -> None:
                repository._branch_state_cache = ValidatedBranchStateCache()
                view = repository.read_world(scope, world_id, branch_id)
                if view.state_hash != initial_hash:
                    raise RuntimeError("The deterministic read fixture changed.")

            repository._branch_state_cache = ValidatedBranchStateCache()
            cold_read_result = _timed(cold_read, samples)

            repository.read_world(scope, world_id, branch_id)
            hot_read_result = _timed(
                lambda: repository.read_world(scope, world_id, branch_id), samples,
            )

            commit_times = []
            for index in range(samples):
                start = perf_counter_ns()
                repository.commit_command(
                    scope,
                    world_id,
                    branch_id,
                    CommandEnvelope(
                        actor_id=actor_id,
                        idempotency_key=f"benchmark-tick-{index + 1}",
                        expected_state_version=index,
                        command_type="core.tick",
                    ),
                    [TimeAdvancedEvent(to_tick=index + 1)],
                )
                commit_times.append(perf_counter_ns() - start)
            commit_result = {
                "samples": len(commit_times),
                "median_ms": _median_ms(commit_times),
                "min_ms": round(min(commit_times) / 1_000_000, 3),
                "max_ms": round(max(commit_times) / 1_000_000, 3),
            }

            checkpoint_times = []
            checkpoints = []
            for tick in range(1, samples, 2):
                start = perf_counter_ns()
                checkpoint = repository.snapshot(scope, world_id, branch_id, at_tick=tick)
                checkpoint_times.append(perf_counter_ns() - start)
                checkpoints.append(checkpoint)
            checkpoint_result = {
                "samples": len(checkpoint_times),
                "median_ms": _median_ms(checkpoint_times),
                "min_ms": round(min(checkpoint_times) / 1_000_000, 3),
                "max_ms": round(max(checkpoint_times) / 1_000_000, 3),
                "stored_checkpoint_count": len(checkpoints),
            }

            head = repository.read_world(scope, world_id, branch_id)
            replay_result = _timed(
                lambda: repository.replay(scope, world_id, branch_id), samples,
            )
            replay_genesis_result = _timed(
                lambda: repository.replay(scope, world_id, branch_id, from_genesis=True),
                max(1, min(samples, 5)),
            )
            last_checkpoint = checkpoints[-1] if checkpoints else repository.snapshot(
                scope, world_id, branch_id, at_tick=0,
            )
            replay_pinned_result = _timed(
                lambda: repository.replay_from_checkpoint(
                    scope,
                    world_id,
                    branch_id,
                    checkpoint_seq=last_checkpoint.last_seq,
                    through_seq=head.last_seq,
                    checkpoint_hash=last_checkpoint.state_hash,
                ),
                samples,
            )

            replay_head = repository.replay(scope, world_id, branch_id)
            pinned_head = repository.replay_from_checkpoint(
                scope,
                world_id,
                branch_id,
                checkpoint_seq=last_checkpoint.last_seq,
                through_seq=head.last_seq,
                checkpoint_hash=last_checkpoint.state_hash,
            )
            if not (replay_head.state_hash == pinned_head.state_hash == head.state_hash):
                raise RuntimeError("Replay paths disagree with the branch head.")

            return {
                "benchmark": "causal-storage",
                "phase": phase,
                "fixture": {
                    "input_state_sha256": initial_hash,
                    "event_count": samples,
                    "world_id": str(world_id),
                    "branch_id": str(branch_id),
                    "entity_count": len(initial.entities),
                    "serialized_state_bytes": initial_bytes,
                    "scratch_database": True,
                },
                "operations": {
                    "snapshot_payload_encoding_microbenchmark": serialization_comparison,
                    "cold_read": cold_read_result,
                    "hot_read": hot_read_result,
                    "commit": commit_result,
                    "explicit_historical_checkpoint": checkpoint_result,
                    "replay_latest_checkpoint": replay_result,
                    "replay_from_genesis": replay_genesis_result,
                    "replay_from_explicit_checkpoint": replay_pinned_result,
                },
                "final_state_sha256": head.state_hash,
                "replay_hashes_match": True,
                "optimization": (
                    "Reuse the canonical state payload already needed for the hash when writing historical snapshots."
                    if phase == "after" else None
                ),
            }
        finally:
            database.dispose()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--phase", required=True, choices=("baseline", "after"))
    parser.add_argument("--samples", type=int, default=12)
    parser.add_argument("--output", type=Path)
    arguments = parser.parse_args()
    if arguments.samples < 4:
        parser.error("--samples must be at least 4 to exercise a historical checkpoint and replay suffix")

    report = run(arguments.phase, arguments.samples)
    output = arguments.output or ROOT / ".runtime" / "benchmarks" / f"causal-storage-{arguments.phase}.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    print(f"report={output}")


if __name__ == "__main__":
    main()
