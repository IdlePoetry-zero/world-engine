from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import sys
from types import SimpleNamespace
from uuid import uuid4

import pytest


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "probe-world-advance.py"
SPEC = importlib.util.spec_from_file_location("probe_world_advance_continuation", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
runner = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = runner
SPEC.loader.exec_module(runner)


def _config(*, days: int) -> dict[str, object]:
    return {
        "pack_id": "test",
        "pack_version": "1.0.0",
        "seed": 17,
        "declared_years": 0,
        "declared_days": days,
        "total_days": days,
        "normalized_years": days // 10,
        "partial_year_days": days % 10,
        "day_seconds": 10,
        "year_days": 10,
        "total_seconds": days * 10,
        "decision_mode": "deterministic_no_model",
        "population_mode": "natural",
        "catalog_seeded": True,
        "seed_semantics": "fixed seed",
    }


class _Repository:
    def __init__(self, manifest: dict[str, object], *, current_tick: int | None = None,
                 current_hash: str | None = None, new_job=None):
        self.world_id = runner.UUID(manifest["world_id"])
        self.branch_id = runner.UUID(manifest["branch_id"])
        self.genesis_tick = int(manifest["start_tick"])
        target = int(manifest["target_tick"])
        cp = runner._terminal_checkpoint(manifest, target_tick=target)
        self.old_job = SimpleNamespace(
            job_id=runner.UUID(manifest["job_id"]),
            world_id=self.world_id,
            branch_id=self.branch_id,
            job_type=runner.JOB_TYPE,
            status=runner.JobStatus.SUCCEEDED,
            payload={
                "plan": {
                    "kind": "world_advance",
                    "started_tick": self.genesis_tick,
                    "target_tick": target,
                },
                "committed_state_version": int(cp["state_version"]),
            },
            committed_through_tick=target,
        )
        self.current_tick = target if current_tick is None else current_tick
        self.current_version = int(cp["state_version"])
        self.current_hash = cp["state_hash"] if current_hash is None else current_hash
        self.head_seq = int(cp["sequence"])
        self.new_job = new_job
        self.branch = SimpleNamespace(
            current_tick=self.current_tick,
            head_seq=self.head_seq,
            state_version=self.current_version,
            state_hash=self.current_hash,
        )
        self.view = SimpleNamespace(
            state=SimpleNamespace(current_tick=self.current_tick),
            branch=self.branch,
            last_seq=self.head_seq,
            state_hash=self.current_hash,
        )
        self.genesis_replay_calls = []
        self.checkpoint_replay_calls = []
        self.checkpoint_replay_result = None
        self.pins_by_sequence = {
            int(pin["sequence"]): dict(pin)
            for pin in manifest.get("checkpoints", [])
            if isinstance(pin, dict) and pin.get("label") == "decade"
        }

    def read_job(self, scope, job_id):
        if job_id == self.old_job.job_id:
            return self.old_job
        if self.new_job is not None and job_id == self.new_job.job_id:
            return self.new_job
        raise AssertionError("unexpected job lookup")

    def find_job_by_key(self, scope, world_id, branch_id, key):
        if key == f"capacity-probe:{self.old_config_hash}":
            return self.old_job
        if self.new_job is not None and key == self.new_key:
            return self.new_job
        return None

    def read_world(self, scope, world_id, branch_id):
        assert world_id == self.world_id and branch_id == self.branch_id
        return self.view

    def replay(self, scope, world_id, branch_id, *, through_seq, from_genesis):
        self.genesis_replay_calls.append((through_seq, from_genesis))
        assert from_genesis is True
        assert through_seq == self.head_seq
        return self.view

    def replay_from_checkpoint(
        self, scope, world_id, branch_id, *, checkpoint_seq, through_seq,
        checkpoint_hash, visit_commit=None,
    ):
        self.checkpoint_replay_calls.append((checkpoint_seq, through_seq, checkpoint_hash))
        pin = self.pins_by_sequence.get(checkpoint_seq)
        if pin is None or pin["state_hash"] != checkpoint_hash:
            raise runner.Conflict("Checkpoint hash does not match the persisted snapshot.")
        if visit_commit is not None:
            visit_commit(checkpoint_seq, SimpleNamespace(current_tick=int(pin["tick"])))
        return self.checkpoint_replay_result or self.view


def _predecessor(tmp_path: Path):
    old_config = _config(days=10)
    config_hash = runner._config_hash(old_config)
    world_id, branch_id, job_id = uuid4(), uuid4(), uuid4()
    start_tick = 1_000
    target_tick = start_tick + old_config["total_seconds"]
    pin_hash = "a" * 64
    terminal = {
        "label": "target",
        "scheduled_year": None,
        "tick": str(target_tick),
        "sequence": "5",
        "state_version": "4",
        "state_hash": pin_hash,
        "counts": {"characters": 1},
    }
    decade = {
        "label": "decade",
        "scheduled_year": 1,
        "tick": str(target_tick),
        "sequence": "5",
        "state_version": "4",
        "state_hash": pin_hash,
        "counts": {"characters": 1},
    }
    manifest = {
        "schema": runner.MANIFEST_SCHEMA,
        "config_hash": config_hash,
        "config": old_config,
        "world_id": str(world_id),
        "branch_id": str(branch_id),
        "job_id": str(job_id),
        "idempotency_key": f"capacity-probe:{config_hash}",
        "status": "succeeded",
        "last_job_status": "succeeded",
        "world_created": True,
        "start_tick": str(start_tick),
        "target_tick": str(target_tick),
        "last_tick": str(target_tick),
        "last_state_version": "4",
        "last_state_hash": pin_hash,
        "checkpoints": [decade, terminal],
        "created_at": "2026-01-01T00:00:00+00:00",
    }
    path = tmp_path / runner.MANIFEST_NAME
    path.write_text(json.dumps(manifest, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    repo = _Repository(manifest)
    repo.old_config_hash = config_hash
    return manifest, repo, path, _config(days=60)


def _predecessor_with_earlier_decades(tmp_path: Path):
    old_config = _config(days=60)
    config_hash = runner._config_hash(old_config)
    world_id, branch_id, job_id = uuid4(), uuid4(), uuid4()
    start_tick = 1_000
    target_tick = start_tick + old_config["total_seconds"]
    terminal_hash = "a" * 64
    checkpoints = []
    for year, sequence, version in ((1, 4, 3), (3, 10, 9), (5, 15, 14)):
        tick = start_tick + year * old_config["year_days"] * old_config["day_seconds"]
        checkpoints.append({
            "label": "decade",
            "scheduled_year": year,
            "tick": str(tick),
            "sequence": str(sequence),
            "state_version": str(version),
            "state_hash": f"{year:064x}",
            "counts": {"characters": 1},
        })
    endpoint_decade = {
        "label": "decade",
        "scheduled_year": old_config["normalized_years"],
        "tick": str(target_tick),
        "sequence": "20",
        "state_version": "19",
        "state_hash": terminal_hash,
        "counts": {"characters": 1},
    }
    checkpoints.extend([
        endpoint_decade,
        {
            "label": "target",
            "scheduled_year": None,
            "tick": str(target_tick),
            "sequence": "20",
            "state_version": "19",
            "state_hash": terminal_hash,
            "counts": {"characters": 1},
        },
    ])
    manifest = {
        "schema": runner.MANIFEST_SCHEMA,
        "config_hash": config_hash,
        "config": old_config,
        "world_id": str(world_id),
        "branch_id": str(branch_id),
        "job_id": str(job_id),
        "idempotency_key": f"capacity-probe:{config_hash}",
        "status": "succeeded",
        "last_job_status": "succeeded",
        "world_created": True,
        "start_tick": str(start_tick),
        "target_tick": str(target_tick),
        "last_tick": str(target_tick),
        "last_state_version": "19",
        "last_state_hash": terminal_hash,
        "checkpoints": checkpoints,
        "created_at": "2026-01-01T00:00:00+00:00",
    }
    path = tmp_path / runner.MANIFEST_NAME
    path.write_text(json.dumps(manifest, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    repo = _Repository(manifest)
    repo.old_config_hash = config_hash
    return manifest, repo, path, _config(days=90)


def _pass_integrity(monkeypatch):
    monkeypatch.setattr(runner, "_sqlite_integrity", lambda _database: {
        "integrity_ok": True, "foreign_keys_ok": True,
    })


def test_cli_has_explicit_completed_continuation_switch():
    parser = runner._build_parser(["test"])
    args = parser.parse_args([
        "--pack", "test", "--seed", "17", "--years", "0", "--days", "60",
        "--data-directory", "probe", "--continue-completed",
    ])
    assert args.continue_completed is True
    assert parser.parse_args([
        "--pack", "test", "--seed", "17", "--years", "0", "--days", "60",
        "--data-directory", "probe",
    ]).continue_completed is False


def test_continuation_archives_completed_manifest_and_starts_from_exact_target(tmp_path, monkeypatch):
    manifest, repository, path, new_config = _predecessor(tmp_path)
    _pass_integrity(monkeypatch)
    original_bytes = path.read_bytes()

    continued, _ = runner._prepare_completed_continuation(
        tmp_path, manifest, new_config, repository=repository, scope=object(),
        world_id=repository.world_id, branch_id=repository.branch_id, database=object(),
    )

    predecessor = continued["predecessor"]
    archive_path = tmp_path / predecessor["manifest_archive"]
    assert archive_path.read_bytes() == original_bytes
    assert json.loads(archive_path.read_text(encoding="utf-8")) == manifest
    assert continued["start_tick"] == manifest["start_tick"]
    assert continued["job_start_tick"] == manifest["target_tick"]
    assert continued["target_tick"] == str(int(manifest["start_tick"]) + new_config["total_seconds"])
    assert continued["idempotency_key"] != manifest["idempotency_key"]
    assert continued["checkpoints"] == manifest["checkpoints"]
    assert predecessor["job_id"] == manifest["job_id"]
    assert predecessor["terminal_checkpoint"] == runner._terminal_checkpoint(
        manifest, target_tick=int(manifest["target_tick"]),
    )
    assert continued["status"] == "initializing"
    assert continued["job_id"] is None
    assert repository.genesis_replay_calls == [(
        int(runner._terminal_checkpoint(
            manifest, target_tick=int(manifest["target_tick"]),
        )["sequence"]),
        True,
    )]


def test_completed_predecessor_uses_latest_strictly_prior_decade_pin(tmp_path, monkeypatch):
    manifest, repository, _path, new_config = _predecessor_with_earlier_decades(tmp_path)
    _pass_integrity(monkeypatch)

    continued, _ = runner._prepare_completed_continuation(
        tmp_path, manifest, new_config, repository=repository, scope=object(),
        world_id=repository.world_id, branch_id=repository.branch_id, database=object(),
    )

    latest_prior = manifest["checkpoints"][2]
    terminal_pin = runner._terminal_checkpoint(
        manifest, target_tick=int(manifest["target_tick"]),
    )
    assert repository.checkpoint_replay_calls == [(
        int(latest_prior["sequence"]),
        int(terminal_pin["sequence"]),
        latest_prior["state_hash"],
    )]
    assert repository.genesis_replay_calls == []
    assert continued["predecessor"]["terminal_checkpoint"]["tick"] == manifest["target_tick"]


def test_same_tick_prior_sequence_is_not_used_as_a_replay_anchor(tmp_path, monkeypatch):
    manifest, repository, _path, new_config = _predecessor(tmp_path)
    endpoint = manifest["checkpoints"][0]
    endpoint.update(sequence="4", state_version="3", state_hash="c" * 64)
    repository = _Repository(manifest)
    repository.old_config_hash = manifest["config_hash"]
    _pass_integrity(monkeypatch)

    runner._prepare_completed_continuation(
        tmp_path, manifest, new_config, repository=repository, scope=object(),
        world_id=repository.world_id, branch_id=repository.branch_id, database=object(),
    )

    assert repository.checkpoint_replay_calls == []
    assert repository.genesis_replay_calls == [(
        int(runner._terminal_checkpoint(
            manifest, target_tick=int(manifest["target_tick"]),
        )["sequence"]),
        True,
    )]


@pytest.mark.parametrize(
    "mutate",
    [
        lambda pin: pin.update(tick="1234"),
        lambda pin: pin.update(sequence="20"),
        lambda pin: pin.update(state_version="19"),
        lambda pin: pin.update(state_hash="not-a-hash"),
    ],
)
def test_completed_predecessor_rejects_malformed_prior_decade_anchor(tmp_path, monkeypatch, mutate):
    manifest, repository, _path, new_config = _predecessor_with_earlier_decades(tmp_path)
    mutate(manifest["checkpoints"][2])
    repository = _Repository(manifest)
    repository.old_config_hash = manifest["config_hash"]
    _pass_integrity(monkeypatch)

    with pytest.raises(runner.ProbeError, match="decade checkpoint"):
        runner._prepare_completed_continuation(
            tmp_path, manifest, new_config, repository=repository, scope=object(),
            world_id=repository.world_id, branch_id=repository.branch_id, database=object(),
        )
    assert repository.checkpoint_replay_calls == []
    assert repository.genesis_replay_calls == []
    assert list(tmp_path.glob(".probe-world-advance.completed.*.json")) == []


def test_completed_predecessor_rejects_persisted_anchor_hash_mismatch(tmp_path, monkeypatch):
    manifest, repository, _path, new_config = _predecessor_with_earlier_decades(tmp_path)
    repository.pins_by_sequence[int(manifest["checkpoints"][2]["sequence"])]["state_hash"] = "c" * 64
    _pass_integrity(monkeypatch)

    with pytest.raises(runner.ProbeError, match="does not validate"):
        runner._prepare_completed_continuation(
            tmp_path, manifest, new_config, repository=repository, scope=object(),
            world_id=repository.world_id, branch_id=repository.branch_id, database=object(),
        )
    assert repository.genesis_replay_calls == []
    assert list(tmp_path.glob(".probe-world-advance.completed.*.json")) == []


@pytest.mark.parametrize("field", ["tick", "version", "hash", "sequence"])
def test_checkpoint_range_replay_must_match_exact_terminal_pin(tmp_path, monkeypatch, field):
    manifest, repository, _path, new_config = _predecessor_with_earlier_decades(tmp_path)
    _pass_integrity(monkeypatch)
    terminal_sequence = repository.head_seq
    terminal_version = repository.current_version
    terminal_hash = repository.current_hash
    terminal_tick = repository.current_tick
    replay_view = SimpleNamespace(
        state=SimpleNamespace(current_tick=terminal_tick + (1 if field == "tick" else 0)),
        branch=SimpleNamespace(state_version=terminal_version + (1 if field == "version" else 0)),
        last_seq=terminal_sequence + (1 if field == "sequence" else 0),
        state_hash=("b" * 64 if field == "hash" else terminal_hash),
    )
    repository.checkpoint_replay_result = replay_view

    with pytest.raises(runner.ProbeError, match="does not replay exactly from its saved anchor"):
        runner._prepare_completed_continuation(
            tmp_path, manifest, new_config, repository=repository, scope=object(),
            world_id=repository.world_id, branch_id=repository.branch_id, database=object(),
        )
    assert repository.genesis_replay_calls == []
    assert list(tmp_path.glob(".probe-world-advance.completed.*.json")) == []


def test_retry_after_archive_before_replace_reuses_exact_archive(tmp_path, monkeypatch):
    manifest, repository, path, new_config = _predecessor(tmp_path)
    _pass_integrity(monkeypatch)
    archive_name = runner._archive_completed_manifest(tmp_path, manifest)
    archive_path = tmp_path / archive_name
    archived_bytes = archive_path.read_bytes()
    assert json.loads(path.read_text(encoding="utf-8")) == manifest

    continued, _ = runner._prepare_completed_continuation(
        tmp_path, manifest, new_config, repository=repository, scope=object(),
        world_id=repository.world_id, branch_id=repository.branch_id, database=object(),
    )

    assert (tmp_path / continued["predecessor"]["manifest_archive"]).read_bytes() == archived_bytes
    assert json.loads(path.read_text(encoding="utf-8"))["predecessor"] == continued["predecessor"]


def test_retry_after_manifest_replace_before_enqueue_is_safe(tmp_path, monkeypatch):
    manifest, repository, _path, new_config = _predecessor(tmp_path)
    _pass_integrity(monkeypatch)
    continued, _ = runner._prepare_completed_continuation(
        tmp_path, manifest, new_config, repository=repository, scope=object(),
        world_id=repository.world_id, branch_id=repository.branch_id, database=object(),
    )

    runner._verify_prepared_continuation(
        tmp_path, continued, new_config, repository=repository, scope=object(),
        world_id=repository.world_id, branch_id=repository.branch_id,
    )
    assert continued["job_id"] is None


def test_ordinary_resume_verifies_saved_continuation_without_extending_it(tmp_path, monkeypatch):
    manifest, repository, _path, new_config = _predecessor(tmp_path)
    _pass_integrity(monkeypatch)
    continued, _ = runner._prepare_completed_continuation(
        tmp_path, manifest, new_config, repository=repository, scope=object(),
        world_id=repository.world_id, branch_id=repository.branch_id, database=object(),
    )
    saved_target = continued["target_tick"]

    assert runner._verify_existing_continuation_resume(
        tmp_path, continued, new_config, repository=repository, scope=object(),
        world_id=repository.world_id, branch_id=repository.branch_id,
    ) is True
    assert continued["target_tick"] == saved_target
    with pytest.raises(runner.ProbeError, match="does not match this duration-only request"):
        runner._verify_existing_continuation_resume(
            tmp_path, continued, _config(days=61), repository=repository, scope=object(),
            world_id=repository.world_id, branch_id=repository.branch_id,
        )

    assert runner._verify_existing_continuation_resume(
        tmp_path, manifest, manifest["config"], repository=repository, scope=object(),
        world_id=repository.world_id, branch_id=repository.branch_id,
    ) is False
    assert repository.current_tick == int(manifest["target_tick"])


def test_completed_continuation_retry_allows_its_new_target_checkpoint(tmp_path, monkeypatch):
    manifest, repository, _path, new_config = _predecessor(tmp_path)
    _pass_integrity(monkeypatch)
    continued, _ = runner._prepare_completed_continuation(
        tmp_path, manifest, new_config, repository=repository, scope=object(),
        world_id=repository.world_id, branch_id=repository.branch_id, database=object(),
    )
    target_tick = int(continued["target_tick"])
    terminal_hash = "b" * 64
    new_target_pin = {
        "label": "target",
        "scheduled_year": None,
        "tick": str(target_tick),
        "sequence": "12",
        "state_version": "7",
        "state_hash": terminal_hash,
        "counts": {"characters": 1},
    }
    continued["checkpoints"].append(new_target_pin)
    continued["job_id"] = str(uuid4())
    continued["status"] = "succeeded"
    continued["last_job_status"] = "succeeded"
    continued["last_tick"] = str(target_tick)
    continued["last_state_version"] = "7"
    continued["last_state_hash"] = terminal_hash
    repository.current_tick = target_tick
    repository.current_version = 7
    repository.current_hash = terminal_hash
    repository.head_seq = 12
    repository.branch.current_tick = target_tick
    repository.branch.state_version = 7
    repository.branch.state_hash = terminal_hash
    repository.branch.head_seq = 12
    repository.view.state.current_tick = target_tick
    repository.view.last_seq = 12
    repository.view.state_hash = terminal_hash
    new_job_id = runner.UUID(continued["job_id"])
    repository.new_key = continued["idempotency_key"]
    repository.new_job = SimpleNamespace(
        job_id=new_job_id,
        world_id=repository.world_id,
        branch_id=repository.branch_id,
        job_type=runner.JOB_TYPE,
        status=runner.JobStatus.SUCCEEDED,
        payload={
            "plan": {
                "kind": "world_advance",
                "started_tick": int(manifest["target_tick"]),
                "target_tick": target_tick,
            },
            "initial_state_version": 4,
            "committed_state_version": 7,
        },
        committed_through_tick=target_tick,
    )

    runner._verify_prepared_continuation(
        tmp_path, continued, new_config, repository=repository, scope=object(),
        world_id=repository.world_id, branch_id=repository.branch_id,
    )
    assert runner._preserves_checkpoints(continued["checkpoints"], manifest["checkpoints"])
    assert continued["checkpoints"][0] == manifest["checkpoints"][0]
    assert continued["checkpoints"][1] == manifest["checkpoints"][1]


@pytest.mark.parametrize("status", [runner.JobStatus.QUEUED, runner.JobStatus.RUNNING])
def test_retry_after_enqueue_before_manifest_job_id_is_safe(tmp_path, monkeypatch, status):
    manifest, repository, _path, new_config = _predecessor(tmp_path)
    _pass_integrity(monkeypatch)
    continued, _ = runner._prepare_completed_continuation(
        tmp_path, manifest, new_config, repository=repository, scope=object(),
        world_id=repository.world_id, branch_id=repository.branch_id, database=object(),
    )
    new_key = continued["idempotency_key"]
    repository.new_key = new_key
    repository.new_job = SimpleNamespace(
        job_id=uuid4(), world_id=repository.world_id, branch_id=repository.branch_id,
        job_type=runner.JOB_TYPE, status=status,
        payload={
            "plan": {
                "kind": "world_advance",
                "started_tick": int(manifest["target_tick"]),
                "target_tick": int(continued["target_tick"]),
            },
            "initial_state_version": repository.current_version,
        },
        committed_through_tick=int(manifest["target_tick"]),
    )

    runner._verify_prepared_continuation(
        tmp_path, continued, new_config, repository=repository, scope=object(),
        world_id=repository.world_id, branch_id=repository.branch_id,
    )
    assert continued["job_id"] is None


def test_progressed_queued_continuation_resumes_after_sqlite_maintenance(tmp_path):
    from world_engine.domain.models import AccessScope, CommandEnvelope, NewJob, NewWorld, TimeAdvancedEvent, WorldEntity, WorldState
    from world_engine.storage.database import Database
    from world_engine.storage.repository import WorldRepository

    database = Database(f"sqlite:///{(tmp_path / 'maintenance-resume.db').as_posix()}")
    database.create_schema()
    repository = WorldRepository(database)
    scope = AccessScope(tenant_id=uuid4(), user_id=uuid4())
    actor_id = uuid4()
    start_tick = 1_000
    old_target = start_tick + int(_config(days=10)["total_seconds"])
    try:
        created = repository.create_world(
            scope,
            NewWorld(
                title="Progressed continuation maintenance resume",
                state=WorldState(
                    current_tick=start_tick,
                    player_id=actor_id,
                    entities={actor_id: WorldEntity(id=actor_id, kind="character", name="Probe owner")},
                ),
            ),
        )
        world_id, branch_id = created.world.world_id, created.branch.branch_id
        initial = repository.read_world(scope, world_id, branch_id)

        old_config = _config(days=10)
        old_config_hash = runner._config_hash(old_config)
        old_job = repository.enqueue_job(
            scope,
            NewJob(
                world_id=world_id,
                branch_id=branch_id,
                job_type=runner.JOB_TYPE,
                idempotency_key=f"capacity-probe:{old_config_hash}",
                payload={
                    "plan": {"kind": "world_advance", "started_tick": start_tick, "target_tick": old_target},
                    "initial_state_version": initial.branch.state_version,
                },
            ),
            expected_state_version=initial.branch.state_version,
        )
        old_lease = repository.claim_job("probe-old", job_types=(runner.JOB_TYPE,))
        assert old_lease is not None and old_lease.job_id == old_job.job_id
        repository.commit_command(
            scope,
            world_id,
            branch_id,
            CommandEnvelope(
                actor_id=scope.user_id,
                idempotency_key="old-terminal-window",
                expected_state_version=initial.branch.state_version,
                command_type="simulation.window",
                payload={"job_id": str(old_job.job_id), "next_tick": str(old_target)},
            ),
            [TimeAdvancedEvent(to_tick=old_target)],
            lease=old_lease,
        )
        repository.finish_job(
            old_lease, runner.JobStatus.SUCCEEDED, committed_through_tick=old_target,
        )
        predecessor_view = repository.read_world(scope, world_id, branch_id)
        predecessor_pin = {
            "tick": str(old_target),
            "sequence": str(predecessor_view.branch.head_seq),
            "state_version": str(predecessor_view.branch.state_version),
            "state_hash": predecessor_view.state_hash,
        }

        manifest, _fake_repository, manifest_path, new_config = _predecessor(tmp_path)
        manifest.update({
            "world_id": str(world_id),
            "branch_id": str(branch_id),
            "job_id": str(old_job.job_id),
            "last_tick": str(old_target),
            "last_state_version": predecessor_pin["state_version"],
            "last_state_hash": predecessor_pin["state_hash"],
        })
        manifest["checkpoints"] = [
            {
                "label": "decade",
                "scheduled_year": 1,
                **predecessor_pin,
                "counts": {"characters": 1},
            },
            {
                "label": "target",
                "scheduled_year": None,
                **predecessor_pin,
                "counts": {"characters": 1},
            },
        ]
        manifest_path.write_text(json.dumps(manifest, sort_keys=True, indent=2) + "\n", encoding="utf-8")
        continued, _ = runner._prepare_completed_continuation(
            tmp_path, manifest, new_config, repository=repository, scope=scope,
            world_id=world_id, branch_id=branch_id, database=database,
        )

        target_tick = int(continued["target_tick"])
        continuation_job = repository.enqueue_job(
            scope,
            NewJob(
                world_id=world_id,
                branch_id=branch_id,
                job_type=runner.JOB_TYPE,
                idempotency_key=continued["idempotency_key"],
                payload={
                    "plan": {"kind": "world_advance", "started_tick": old_target, "target_tick": target_tick},
                    "initial_state_version": predecessor_view.branch.state_version,
                    "speed": "balanced",
                    "depth": "deep",
                },
            ),
            expected_state_version=predecessor_view.branch.state_version,
        )
        continued["job_id"] = str(continuation_job.job_id)
        lease = repository.claim_job("probe-continuation", job_types=(runner.JOB_TYPE,))
        assert lease is not None and lease.job_id == continuation_job.job_id
        current = repository.read_world(scope, world_id, branch_id)
        resumed_tick = old_target + 10
        repository.commit_command(
            scope,
            world_id,
            branch_id,
            CommandEnvelope(
                actor_id=scope.user_id,
                idempotency_key="continued-progress-window",
                expected_state_version=current.branch.state_version,
                command_type="simulation.window",
                payload={"job_id": str(continuation_job.job_id), "next_tick": str(resumed_tick)},
            ),
            [TimeAdvancedEvent(to_tick=resumed_tick)],
            lease=lease,
        )
        repository.finish_job(
            lease, runner.JobStatus.PAUSED,
            result={"reason_code": "storage_maintenance"},
            committed_through_tick=resumed_tick,
        )
        queued = repository.control_job(scope, continuation_job.job_id, "resume")
        assert queued.status == runner.JobStatus.QUEUED
        assert queued.committed_through_tick == resumed_tick
        assert queued.payload["committed_state_version"] == predecessor_view.branch.state_version + 1

        runner._verify_prepared_continuation(
            tmp_path, continued, new_config, repository=repository, scope=scope,
            world_id=world_id, branch_id=branch_id,
        )
    finally:
        database.dispose()


@pytest.mark.parametrize("regression", ["tick", "version"])
def test_prepared_continuation_rejects_progress_behind_predecessor_pin(tmp_path, monkeypatch, regression):
    manifest, _repository, _path, new_config = _predecessor(tmp_path)
    _pass_integrity(monkeypatch)
    continued, _ = runner._prepare_completed_continuation(
        tmp_path, manifest, new_config, repository=_repository, scope=object(),
        world_id=_repository.world_id, branch_id=_repository.branch_id, database=object(),
    )
    repository = _Repository(manifest)
    repository.old_config_hash = manifest["config_hash"]
    repository.new_key = continued["idempotency_key"]
    predecessor_tick = int(manifest["target_tick"])
    predecessor_version = int(manifest["last_state_version"])
    current_tick = predecessor_tick - 1 if regression == "tick" else predecessor_tick
    current_version = predecessor_version - 1 if regression == "version" else predecessor_version
    repository.current_tick = current_tick
    repository.current_version = current_version
    repository.branch.current_tick = current_tick
    repository.branch.state_version = current_version
    repository.view.state.current_tick = current_tick
    repository.new_job = SimpleNamespace(
        job_id=uuid4(),
        world_id=repository.world_id,
        branch_id=repository.branch_id,
        job_type=runner.JOB_TYPE,
        status=runner.JobStatus.QUEUED,
        payload={
            "plan": {
                "kind": "world_advance",
                "started_tick": predecessor_tick,
                "target_tick": int(continued["target_tick"]),
            },
            "initial_state_version": predecessor_version,
            "committed_state_version": current_version,
        },
        committed_through_tick=current_tick,
    )

    with pytest.raises(runner.ProbeError, match="progress does not match the current branch head"):
        runner._verify_prepared_continuation(
            tmp_path, continued, new_config, repository=repository, scope=object(),
            world_id=repository.world_id, branch_id=repository.branch_id,
        )


@pytest.mark.parametrize(
    "change",
    [
        {"current_tick": 1_101},
        {"current_hash": "b" * 64},
    ],
)
def test_continue_refuses_changed_branch_head_without_archiving(tmp_path, monkeypatch, change):
    manifest, repository, _path, new_config = _predecessor(tmp_path)
    repository = _Repository(manifest, **change)
    repository.old_config_hash = manifest["config_hash"]
    _pass_integrity(monkeypatch)

    with pytest.raises(runner.ProbeError, match="terminal checkpoint"):
        runner._prepare_completed_continuation(
            tmp_path, manifest, new_config, repository=repository, scope=object(),
            world_id=repository.world_id, branch_id=repository.branch_id, database=object(),
        )
    assert list(tmp_path.glob(".probe-world-advance.completed.*.json")) == []


def test_continue_rejects_non_duration_change_and_nonincreasing_duration(tmp_path, monkeypatch):
    manifest, repository, _path, new_config = _predecessor(tmp_path)
    _pass_integrity(monkeypatch)
    changed_seed = {**new_config, "seed": 18}
    shorter = _config(days=9)

    for candidate in (changed_seed, shorter):
        with pytest.raises(runner.ProbeError, match="only increase"):
            runner._prepare_completed_continuation(
                tmp_path, manifest, candidate, repository=repository, scope=object(),
                world_id=repository.world_id, branch_id=repository.branch_id, database=object(),
            )
    assert list(tmp_path.glob(".probe-world-advance.completed.*.json")) == []


@pytest.mark.parametrize("status", [runner.JobStatus.QUEUED, runner.JobStatus.RUNNING])
def test_continue_never_takes_over_a_nonsucceeded_predecessor_job(tmp_path, monkeypatch, status):
    manifest, repository, _path, new_config = _predecessor(tmp_path)
    repository.old_job.status = status
    _pass_integrity(monkeypatch)

    with pytest.raises(runner.ProbeError, match="stored predecessor job"):
        runner._prepare_completed_continuation(
            tmp_path, manifest, new_config, repository=repository, scope=object(),
            world_id=repository.world_id, branch_id=repository.branch_id, database=object(),
        )
    assert list(tmp_path.glob(".probe-world-advance.completed.*.json")) == []


def test_continue_rejects_a_predecessor_job_from_another_world(tmp_path, monkeypatch):
    manifest, repository, _path, new_config = _predecessor(tmp_path)
    repository.old_job.world_id = uuid4()
    _pass_integrity(monkeypatch)

    with pytest.raises(runner.ProbeError, match="stored predecessor job"):
        runner._prepare_completed_continuation(
            tmp_path, manifest, new_config, repository=repository, scope=object(),
            world_id=repository.world_id, branch_id=repository.branch_id, database=object(),
        )
    assert list(tmp_path.glob(".probe-world-advance.completed.*.json")) == []

