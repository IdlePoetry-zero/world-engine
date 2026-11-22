from copy import deepcopy
import importlib.util
import hashlib
import json
from pathlib import Path
import sys
from uuid import uuid4

import pytest


SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "merge-capacity-intervals.py"
SPEC = importlib.util.spec_from_file_location("merge_capacity_intervals", SCRIPT)
merge = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(merge)


def _pin(sequence, tick, year, state_version=None):
    return {"sequence": str(sequence), "tick": str(tick), "year": year,
            "state_version": str(sequence if state_version is None else state_version),
            "state_hash": f"{sequence:064x}"}


def _episode(first, last, minimum, count, *, left=False, right=False):
    return {
        "start_sequence": first["sequence"], "start_tick": first["tick"],
        "end_sequence": last["sequence"], "end_tick": last["tick"],
        "minimum_count": minimum, "left_censored": left, "right_censored": right,
        "deficient_commit_count": count,
        "duration_seconds_observed": int(last["tick"]) - int(first["tick"]),
    }


def _report(start, end, count, minimum, trough, episodes):
    return {
        "audit": "capacity_interval_population", "schema": 1, "status": "verified", "success": True,
        "scope": dict(world_id="world", branch_id="branch", verified_root_genesis_hash="root",
                      original_key_identity_source="genesis"),
        "interval": dict(start_checkpoint=start, end_checkpoint=end, commit_boundary_sequence_verified=True,
                         expected_commit_boundary_count=count, observed_commit_boundary_count=count,
                         first_sequence=start["sequence"], last_sequence=end["sequence"],
                         calendar_basis={"year_days": 360, "day_seconds": 1}),
        "population": {
            "definitions": {"ordinary": "fixture"},
            "minimum_counts": {merge.ORDINARY_COUNT: {
                "count": minimum, "sequence": trough["sequence"], "tick": trough["tick"],
            }},
            "ordinary_population_shortfalls": dict(threshold=240, episodes=episodes,
                                                  deficient_commit_count=sum(row["deficient_commit_count"] for row in episodes)),
        },
    }


def _prefix(pin, *, minimum=250, first_below=None, open_gap=True):
    metric = "active_named_residents_excluding_player_and_authored_keys"
    coverage_flags = {
        key: True for key in merge._PREFIX_COVERAGE_FLAGS
    }
    coverage_flags.update(
        committed_states_audited=3,
        distinct_sequences_audited=3,
        expected_committed_states_including_initial=3,
        database_or_manifest_mutated=False,
        model_calls=0,
        sqlite_open_mode="uri_mode_ro_with_query_only",
    )
    pin_row = {key: pin[key] for key in ("sequence", "tick", "state_version", "state_hash")}
    population_minima = {
        "active_named_people_including_player": {"count": 297, "sequence": "0", "tick": "0"},
        "active_named_people_excluding_player": {"count": 296, "sequence": "0", "tick": "0"},
        metric: {"count": minimum, "sequence": "0", "tick": "0"},
    }
    first_by_metric = {
        "active_named_people_including_player": None,
        "active_named_people_excluding_player": None,
        metric: first_below,
    }
    return {
        "audit": "world_capacity_genesis_replay", "schema": 1,
        "status": "checkpoint_prefix_verified", "success": True, "replay_verified": True,
        "scope": {
            "audit_mode": "checkpoint_prefix", "world_id": "world", "branch_id": "branch",
            "owner_matches": True, "checkpoint_year": pin["year"],
            "audited_through_sequence": pin["sequence"], "audited_through_tick": pin["tick"],
            "pinned_checkpoint": pin_row,
        },
        "root_genesis": {
            "branch_id": "branch", "calendar": {"day_seconds": 1, "year_days": 360},
            "current_tick": "0", "state_hash": "root", "state_version": 0,
        },
        "time": {
            "audited_boundary_replayed_and_stored": True, "audited_boundary_tick": pin["tick"],
            "calendar_basis": {"day_seconds": 1, "year_days": 360},
        },
        "coverage": coverage_flags,
        "capacity": {
            "audit_interval_scope": "checkpoint_prefix", "audited_through_year": pin["year"],
            "threshold_active_named_residents": 240,
            "minimum_counts": population_minima,
            "first_below_240_by_definition": first_by_metric,
            "first_below_240": first_below,
        },
        "structural_capacity": {
            "audit_interval_scope": "checkpoint_prefix",
            "thresholds": {"active_countries": 24, "active_other_organizations": 24, "active_places": 120},
            "minimum_counts": {
                "active_countries": {"count": 24, "sequence": "0", "tick": "0"},
                "active_other_organizations": {"count": 24, "sequence": "0", "tick": "0"},
                "active_places": {"count": 120, "sequence": "0", "tick": "0"},
            },
            "continuous_checks_over_replayed_history": {
                "active_countries": {"minimum_count": 24, "threshold": 24, "passed_continuously": True},
                "active_other_organizations": {"minimum_count": 24, "threshold": 24, "passed_continuously": True},
                "active_places": {"minimum_count": 120, "threshold": 120, "passed_continuously": True},
            },
        },
        "institutional_continuity": {
            "interval_scope": "checkpoint_prefix", "pin_matches_last_committed_sample": True,
            "audited_from_sequence": "0", "audited_from_tick": "0",
            "audited_through_sequence": pin["sequence"], "audited_through_tick": pin["tick"],
            "organizations": [{
                "organization_id": "guild", "organization_name": "Guild", "open_at_pin": open_gap,
                "zero_member_gap_episode_count": 1 if open_gap else 0,
                "total_positive_gap_seconds": 10 if open_gap else 0,
                "zero_duration_same_tick_episode_count": 0,
                "longest_zero_member_gap": 10 if open_gap else None,
            }],
        },
    }


def _with_suffix_coverage(report, *, start, end, count, left_open=True, structural_counts=None):
    report["scope"].update(institutional_metrics_audited=True, population_only=False)
    thresholds = {"active_countries": 24, "active_other_organizations": 24, "active_places": 120}
    minima = {}
    checks = {}
    for metric, threshold in thresholds.items():
        count_value = (structural_counts or {}).get(metric, threshold)
        minima[metric] = {"count": count_value, "sequence": start["sequence"], "tick": start["tick"]}
        checks[metric] = {
            "minimum_count": count_value, "threshold": threshold,
            "passed_continuously": count_value >= threshold,
        }
    gap = {
        "start_sequence": start["sequence"], "start_tick": start["tick"],
        "end_sequence": end["sequence"], "end_tick": end["tick"],
        "minimum_count": 0, "left_censored": left_open, "right_censored": True,
        "observed_zero_member_committed_state_count": count,
        "duration_seconds_observed": int(end["tick"]) - int(start["tick"]),
        "end_reason": "interval_end", "last_zero_member_sequence": end["sequence"],
        "last_zero_member_tick": end["tick"],
    }
    report["institutions"] = {
        "audited_from_sequence": start["sequence"], "audited_from_tick": start["tick"],
        "audited_through_sequence": end["sequence"], "audited_through_tick": end["tick"],
        "pin_matches_last_committed_sample": True,
        "censoring": {"observed_commit_boundary_count": count},
        "organizations": [{
            "organization_id": "guild", "organization_name": "Guild",
            "left_censored_at_interval_start": left_open,
            "right_censored_at_interval_end": True, "gap_windows": [gap] if left_open else [],
        }],
        "structural_capacity": {
            "observed_commit_boundary_count": count, "thresholds": thresholds,
            "minimum_counts": minima,
            "continuous_checks_over_replayed_interval": checks,
        },
    }
    return report


def test_merge_keeps_one_shortfall_across_two_shared_pins_and_deduplicates_counts():
    start, decline, seam1 = _pin(100, 1000, 10), _pin(101, 1002, 10), _pin(102, 2000, 20)
    seam2, recovery, end = _pin(104, 3000, 30), _pin(105, 3000, 30), _pin(106, 4000, 40)
    first = _report(start, seam1, 3, 238, seam1, [_episode(decline, seam1, 238, 2, right=True)])
    middle = _report(seam1, seam2, 3, 237, seam2, [_episode(seam1, seam2, 237, 3, left=True, right=True)])
    last = _report(seam2, end, 3, 237, seam2, [_episode(seam2, recovery, 237, 1, left=True)])
    result = merge.merge_intervals([last, first, middle])
    assert result["observed_distinct_commit_boundary_count"] == 7
    assert result["ordinary_population_shortfalls"]["deficient_commit_count"] == 4
    assert result["ordinary_population_shortfalls"]["episodes"] == [_episode(decline, recovery, 237, 4)]
    assert result["minimum_counts"][merge.ORDINARY_COUNT]["sequence"] == seam2["sequence"]


def test_merge_rejects_mismatched_pin_and_deficient_legacy_report():
    start, common, end = _pin(40, 1000, 10), _pin(41, 2000, 20), _pin(42, 3000, 30)
    first = _report(start, common, 2, 240, common, [])
    second = _report(common, end, 2, 240, common, [])
    del first["population"]["ordinary_population_shortfalls"]
    assert merge.merge_intervals([first, second])["ordinary_population_shortfalls"]["episodes"] == []

    mismatched = deepcopy(second)
    mismatched["interval"]["start_checkpoint"]["state_hash"] = "another-pin"
    with pytest.raises(merge.MergeError, match="exact saved pin"):
        merge.merge_intervals([first, mismatched])

    first["population"]["minimum_counts"][merge.ORDINARY_COUNT]["count"] = 239
    with pytest.raises(merge.MergeError, match="re-audited"):
        merge.merge_intervals([first, second])


def test_merge_rejects_inconsistent_membership_at_the_shared_pin():
    start, common, end = _pin(40, 1000, 10), _pin(41, 2000, 20), _pin(42, 3000, 30)
    first = _report(start, common, 2, 240, common, [])
    second = _report(common, end, 2, 239, common, [_episode(common, end, 239, 1, left=True)])
    with pytest.raises(merge.MergeError, match="shared state was below"):
        merge.merge_intervals([first, second])


def test_merge_joins_the_same_organization_gap_across_a_saved_pin():
    start, decline, seam = _pin(100, 1000, 10), _pin(101, 1002, 10), _pin(102, 2000, 20)
    recovery, end = _pin(103, 2300, 20), _pin(104, 3000, 30)
    first = _report(start, seam, 3, 240, start, [])
    second = _report(seam, end, 3, 240, seam, [])
    for report, window in (
        (first, _episode(decline, seam, 0, 2, right=True)),
        (second, _episode(seam, recovery, 0, 1, left=True)),
    ):
        window["observed_zero_member_committed_state_count"] = window.pop("deficient_commit_count")
        window["last_zero_member_sequence"], window["last_zero_member_tick"] = seam["sequence"], seam["tick"]
        window["end_reason"] = "interval_end" if window["right_censored"] else "active_named_membership_recovered"
        report["institutions"] = {
            "pin_matches_last_committed_sample": True,
            "censoring": {"observed_commit_boundary_count": 3},
            "organizations": [{"organization_id": "guild", "organization_name": "Guild", "gap_windows": [window]}],
        }
    result = merge.merge_intervals([first, second])
    guild, = result["organization_membership_gaps"]["organizations_with_gaps"]
    gap, = guild["gap_windows"]
    assert gap["duration_seconds_observed"] == 1298
    assert gap["observed_zero_member_committed_state_count"] == 2
    assert gap["end_reason"] == "active_named_membership_recovered"
    assert not gap["left_censored"] and not gap["right_censored"]


def _prefix_suffix_fixture(*, ordinary_minimum=235, suffix_left_open=True, structural_counts=None):
    start = _pin(100, 3600, 10, state_version=2)
    trough = _pin(102, 5400, 15, state_version=3)
    end = _pin(104, 7200, 20, state_version=4)
    prefix = _prefix(start)
    episodes = [_episode(trough, end, ordinary_minimum, 2)] if ordinary_minimum < 240 else []
    report = _report(start, end, 3, ordinary_minimum, trough, episodes)
    report["scope"].update(world_id="world", branch_id="branch", verified_root_genesis_hash="root")
    if ordinary_minimum >= 240:
        report["population"].pop("ordinary_population_shortfalls")
    _with_suffix_coverage(
        report, start=start, end=end, count=3,
        left_open=suffix_left_open, structural_counts=structural_counts,
    )
    return prefix, report


def test_prefix_join_counts_boundaries_and_keeps_organization_sources_separate():
    prefix, report = _prefix_suffix_fixture()
    result = merge.merge_prefix_and_intervals(prefix, [report])

    assert result["observed_distinct_commit_boundary_count"] == 5
    assert result["observed_distinct_commit_boundary_count"] == (
        int(result["end_checkpoint"]["state_version"]) - int(result["start_checkpoint"]["state_version"]) + 1
    )
    assert result["ordinary_population"]["minimum_count"] == {
        "count": 235, "sequence": "102", "tick": "5400",
    }
    assert result["ordinary_population"]["first_below_240"] == {
        "sequence": "102", "tick": "5400", "count_at_first_below": None,
        "episode_minimum_count": 235,
    }
    assert result["ordinary_population"]["first_below_source"] == "suffix_interval_chain"
    assert result["structural_capacity"]["qualified"] is True
    assert result["structural_capacity"]["thresholds_passed_over_replayed_history"] is True
    assert result["organization_membership_coverage"]["prefix_aggregate"]["organizations"][0]["open_at_pin"] is True
    assert result["organization_membership_coverage"]["suffix_windows"]["coverage_available"] is True
    assert result["organization_membership_coverage"]["shared_pin_open_state_comparison"]["all_compared_states_match"] is True
    assert result["population_600_year_passed"] is False
    assert result["durable_job_completion_verified"] is False


def test_prefix_join_requires_exact_checkpoint_hash_and_year():
    prefix, report = _prefix_suffix_fixture()
    report["interval"]["start_checkpoint"]["state_hash"] = "different"
    with pytest.raises(merge.MergeError, match="exact saved pin"):
        merge.merge_prefix_and_intervals(prefix, [report])

    prefix, report = _prefix_suffix_fixture()
    report["interval"]["start_checkpoint"]["year"] = 11
    with pytest.raises(merge.MergeError, match="exact saved pin"):
        merge.merge_prefix_and_intervals(prefix, [report])

    prefix, report = _prefix_suffix_fixture()
    report["interval"]["start_checkpoint"]["state_version"] = "3"
    with pytest.raises(merge.MergeError, match="exact saved pin"):
        merge.merge_prefix_and_intervals(prefix, [report])


def test_prefix_join_rejects_bad_coverage_flags_and_organization_seam():
    prefix, report = _prefix_suffix_fixture()
    prefix["coverage"]["root_initial_state_hash_verified"] = False
    with pytest.raises(merge.MergeError, match="coverage flag"):
        merge.merge_prefix_and_intervals(prefix, [report])

    prefix, report = _prefix_suffix_fixture()
    report["institutions"]["organizations"][0]["left_censored_at_interval_start"] = False
    with pytest.raises(merge.MergeError, match="organization membership states differ"):
        merge.merge_prefix_and_intervals(prefix, [report])


def test_prefix_join_rejects_deficient_legacy_interval_without_shortfall_windows():
    prefix, report = _prefix_suffix_fixture()
    report["population"].pop("ordinary_population_shortfalls")
    with pytest.raises(merge.MergeError, match="re-audited"):
        merge.merge_prefix_and_intervals(prefix, [report])


def test_prefix_first_below_precedes_suffix_minimum_evidence():
    start, trough, end = (
        _pin(100, 3600, 10, state_version=2),
        _pin(102, 5400, 15, state_version=3),
        _pin(104, 7200, 20, state_version=4),
    )
    first = {"count": 239, "sequence": "80", "tick": "2800"}
    prefix = _prefix(start, minimum=235, first_below=first)
    suffix = _report(start, end, 3, 245, trough, [])
    suffix["scope"].update(world_id="world", branch_id="branch", verified_root_genesis_hash="root")
    _with_suffix_coverage(suffix, start=start, end=end, count=3)

    result = merge.merge_prefix_and_intervals(prefix, [suffix])

    assert result["ordinary_population"]["minimum_count"] == {
        "count": 235, "sequence": "0", "tick": "0",
    }
    assert result["ordinary_population"]["first_below_240"] == {
        "count": 239, "sequence": "80", "tick": "2800",
    }
    assert result["ordinary_population"]["first_below_source"] == "genesis_prefix"


def test_prefix_join_checks_root_to_end_version_span():
    prefix, report = _prefix_suffix_fixture()
    report["interval"]["end_checkpoint"]["state_version"] = "5"
    with pytest.raises(merge.MergeError, match="state-version span"):
        merge.merge_prefix_and_intervals(prefix, [report])


def test_prefix_cli_accepts_one_interval_and_records_both_input_hashes(tmp_path, monkeypatch):
    prefix, report = _prefix_suffix_fixture()
    prefix_path = tmp_path / "prefix.json"
    interval_path = tmp_path / "interval.json"
    output_path = tmp_path / "joined.json"
    prefix_bytes = json.dumps(prefix).encode("utf-8")
    interval_bytes = json.dumps(report).encode("utf-8")
    prefix_path.write_bytes(prefix_bytes)
    interval_path.write_bytes(interval_bytes)
    monkeypatch.setattr(sys, "argv", [
        str(SCRIPT), "--genesis-prefix", str(prefix_path), str(interval_path), "--output", str(output_path),
    ])

    merge.main()

    result = json.loads(output_path.read_text(encoding="utf-8"))
    assert result["genesis_prefix_report"]["sha256"] == hashlib.sha256(prefix_bytes).hexdigest()
    assert result["input_reports"] == [{
        "path": str(interval_path), "sha256": hashlib.sha256(interval_bytes).hexdigest(),
    }]


@pytest.mark.parametrize("target", ["prefix", "interval"])
def test_prefix_cli_refuses_to_overwrite_either_input(tmp_path, monkeypatch, target):
    prefix, report = _prefix_suffix_fixture()
    prefix_path = tmp_path / "prefix.json"
    interval_path = tmp_path / "interval.json"
    prefix_path.write_text(json.dumps(prefix), encoding="utf-8")
    interval_path.write_text(json.dumps(report), encoding="utf-8")
    destination = prefix_path if target == "prefix" else interval_path
    before = destination.read_bytes()
    monkeypatch.setattr(sys, "argv", [
        str(SCRIPT), "--genesis-prefix", str(prefix_path), str(interval_path), "--output", str(destination),
    ])
    with pytest.raises(SystemExit) as error:
        merge.main()
    assert error.value.code == 2
    assert destination.read_bytes() == before


def test_no_prefix_cli_keeps_existing_chain_schema(tmp_path, monkeypatch):
    start, seam, end = _pin(40, 1000, 10), _pin(41, 2000, 20), _pin(42, 3000, 30)
    first = _report(start, seam, 2, 240, start, [])
    second = _report(seam, end, 2, 240, seam, [])
    first_path, second_path = tmp_path / "first.json", tmp_path / "second.json"
    output_path = tmp_path / "merged.json"
    first_path.write_text(json.dumps(first), encoding="utf-8")
    second_path.write_text(json.dumps(second), encoding="utf-8")
    monkeypatch.setattr(sys, "argv", [str(SCRIPT), str(first_path), str(second_path), "--output", str(output_path)])

    merge.main()

    result = json.loads(output_path.read_text(encoding="utf-8"))
    assert set(result) == set(merge.merge_intervals([first, second])) | {"input_reports"}
    assert "genesis_prefix_report" not in result


def _terminal_probe_fixture(
    tmp_path,
    *,
    finish_terminal_job=True,
    terminal_year=20,
    prefix_minimum=234,
    suffix_minimum=250,
    structural_counts=None,
    manifest_status=None,
    manifest_last_job_status=None,
    include_target_checkpoint=True,
    authored_key_count=32,
):
    from world_engine.domain.models import (
        AccessScope, CommandEnvelope, JobStatus, NewJob, NewWorld, TimeAdvancedEvent, WorldEntity, WorldState,
    )
    from world_engine.simulation.contracts import WorldAdvancePlan
    from world_engine.storage.database import Database
    from world_engine.storage.repository import WorldRepository

    data_directory = tmp_path / "probe"
    data_directory.mkdir()
    database_path = data_directory / "world-engine.db"
    database = Database(f"sqlite:///{database_path.as_posix()}")
    database.create_schema()
    repository = WorldRepository(database)
    scope = AccessScope(tenant_id=uuid4(), user_id=uuid4())
    authored_keys = [uuid4() for _ in range(authored_key_count)]
    initial = WorldState(
        calendar={"day_seconds": 1, "year_days": 360},
        entities={
            ident: WorldEntity(id=ident, kind="character", name=f"Key profile {index}")
            for index, ident in enumerate(authored_keys)
        },
        metadata={
            "content_index": {"key_npc_ids": [str(ident) for ident in authored_keys], "other_resident_ids": []},
            "seed_counts": {"key_npcs": authored_key_count, "other_named_living_characters": 0},
        },
    )
    created = repository.create_world(scope, NewWorld(title="Terminal verification fixture", state=initial))
    world_id, branch_id = created.world.world_id, created.branch.branch_id

    def commit_advance(start_tick, target_tick, key, *, finish=True):
        view = repository.read_world(scope, world_id, branch_id)
        job = repository.enqueue_job(
            scope,
            NewJob(
                world_id=world_id, branch_id=branch_id, job_type="simulation.world_advance",
                idempotency_key=key,
                payload={"plan": WorldAdvancePlan(started_tick=start_tick, target_tick=target_tick).model_dump(mode="json")},
            ),
            expected_state_version=view.branch.state_version,
        )
        lease = repository.claim_job(key, job_types=("simulation.world_advance",))
        assert lease is not None and lease.job_id == job.job_id
        repository.commit_command(
            scope, world_id, branch_id,
            CommandEnvelope(
                actor_id=scope.user_id, idempotency_key=f"{key}-window",
                expected_state_version=view.branch.state_version, command_type="simulation.window",
                payload={"job_id": str(job.job_id), "next_tick": str(target_tick)},
            ),
            [TimeAdvancedEvent(to_tick=target_tick)], lease=lease,
        )
        if finish:
            repository.finish_job(lease, JobStatus.SUCCEEDED, committed_through_tick=target_tick)
        return repository.read_job(scope, job.job_id)

    commit_advance(0, 3600, "fixture-decade-10")
    first_view = repository.read_world(scope, world_id, branch_id)
    first_pin = {
        "sequence": str(first_view.last_seq), "tick": str(first_view.state.current_tick),
        "year": 10, "state_version": str(first_view.branch.state_version),
        "state_hash": first_view.state_hash,
    }
    terminal_tick = terminal_year * 360
    terminal_job = commit_advance(
        3600, terminal_tick, f"fixture-terminal-decade-{terminal_year}", finish=finish_terminal_job,
    )
    terminal_view = repository.read_world(scope, world_id, branch_id)
    end_pin = {
        "sequence": str(terminal_view.last_seq), "tick": str(terminal_view.state.current_tick),
        "year": terminal_year, "state_version": str(terminal_view.branch.state_version),
        "state_hash": terminal_view.state_hash,
    }
    readonly_engine = merge._audit_helpers()._readonly_engine(database_path)
    try:
        root_genesis, _ = merge._audit_helpers()._root_genesis(readonly_engine, world_id, branch_id)
    finally:
        readonly_engine.dispose()

    manifest_status = manifest_status or terminal_job.status.value
    manifest_last_job_status = manifest_last_job_status or (
        "running" if manifest_status == "running" else terminal_job.status.value
    )
    final_manifest_pin = (
        first_pin if manifest_status == "running" or manifest_last_job_status in {"running", "paused"}
        else end_pin
    )
    manifest = {
        "schema": 1, "database_file": "world-engine.db", "world_created": True,
        "world_id": str(world_id), "branch_id": str(branch_id), "job_id": str(terminal_job.job_id),
        "start_tick": "0", "target_tick": str(terminal_tick),
        "config": {"total_seconds": terminal_tick, "day_seconds": 1, "year_days": 360},
        "status": manifest_status,
        "last_job_status": manifest_last_job_status,
        "last_tick": end_pin["tick"] if manifest_status == "succeeded" and manifest_last_job_status in {"running", "paused"}
        else final_manifest_pin["tick"],
        "last_state_version": final_manifest_pin["state_version"],
        "last_state_hash": final_manifest_pin["state_hash"],
        "checkpoints": [
            {"label": "decade", "scheduled_year": 10,
             **{key: first_pin[key] for key in ("tick", "sequence", "state_version", "state_hash")}},
            {"label": "decade", "scheduled_year": terminal_year,
             **{key: end_pin[key] for key in ("tick", "sequence", "state_version", "state_hash")}},
        ],
    }
    if include_target_checkpoint:
        manifest["checkpoints"].append({
            "label": "target", "scheduled_year": None,
            **{key: end_pin[key] for key in ("tick", "sequence", "state_version", "state_hash")},
        })
    manifest_path = data_directory / ".probe-world-advance.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    (data_directory / "local-identity.json").write_text(
        json.dumps({"tenant_id": str(scope.tenant_id), "user_id": str(scope.user_id)}), encoding="utf-8",
    )

    first_below = {"count": prefix_minimum, "sequence": "0", "tick": "0"} if prefix_minimum < 240 else None
    prefix = _prefix(first_pin, minimum=prefix_minimum, first_below=first_below)
    prefix["scope"].update(world_id=str(world_id), branch_id=str(branch_id))
    prefix["root_genesis"].update(branch_id=str(branch_id), state_hash=root_genesis["state_hash"])
    prefix["coverage"].update(
        committed_states_audited=2, distinct_sequences_audited=2,
        expected_committed_states_including_initial=2,
    )
    episodes = [_episode(end_pin, end_pin, suffix_minimum, 1, right=True)] if suffix_minimum < 240 else []
    report = _report(first_pin, end_pin, 2, suffix_minimum, first_pin, episodes)
    report["scope"].update(
        world_id=str(world_id), branch_id=str(branch_id),
        verified_root_genesis_hash=root_genesis["state_hash"],
    )
    _with_suffix_coverage(
        report, start=first_pin, end=end_pin, count=2, structural_counts=structural_counts,
    )
    return data_directory, prefix, report, manifest_path


def test_terminal_read_only_proof_is_independent_of_small_population_result(tmp_path):
    data_directory, prefix, report, _manifest_path = _terminal_probe_fixture(tmp_path)

    result = merge.merge_prefix_and_intervals(prefix, [report])
    merge._attach_terminal_data_proof(result, prefix, data_directory)

    assert result["durable_job_completion_verified"] is True
    assert result["terminal_data_proof"]["verified"] is True
    assert result["terminal_data_proof"]["terminal_only"] is True
    assert result["terminal_data_proof"]["historical_replay_performed"] is False
    assert result["terminal_data_proof"]["job_started_tick"] == 3600
    assert result["terminal_data_proof"]["original_key_profile_at_genesis"]["passed_at_genesis"] is True
    assert result["ordinary_population"]["minimum_count"]["count"] == 234
    assert result["population_600_year_passed"] is False


def test_terminal_proof_passes_exact_600_year_population_and_key_capacity_gates(tmp_path):
    data_directory, prefix, report, _manifest_path = _terminal_probe_fixture(
        tmp_path, terminal_year=600, prefix_minimum=250,
        manifest_status="succeeded", manifest_last_job_status="paused",
    )

    result = merge.merge_prefix_and_intervals(prefix, [report])
    merge._attach_terminal_data_proof(result, prefix, data_directory)

    assert result["durable_job_completion_verified"] is True
    assert result["terminal_data_proof"]["manifest_receipt_form"] == (
        "persisted_target_checkpoint_before_consolidated_job_receipt"
    )
    assert result["terminal_data_proof"]["manifest_consolidated_fields_match_head"] is False
    assert result["population_600_year_passed"] is True
    assert result["organization_membership_coverage"]["shared_pin_open_state_comparison"][
        "all_compared_states_match"
    ] is True


@pytest.mark.parametrize("failure", ["ordinary_population", "structural_threshold", "key_profile"])
def test_terminal_proof_keeps_exact_600_population_gate_false_on_threshold_failure(tmp_path, failure):
    data_directory, prefix, report, _manifest_path = _terminal_probe_fixture(
        tmp_path,
        terminal_year=600,
        prefix_minimum=250,
        suffix_minimum=239 if failure == "ordinary_population" else 250,
        structural_counts={"active_countries": 23} if failure == "structural_threshold" else None,
        authored_key_count=31 if failure == "key_profile" else 32,
    )

    result = merge.merge_prefix_and_intervals(prefix, [report])
    merge._attach_terminal_data_proof(result, prefix, data_directory)

    assert result["durable_job_completion_verified"] is True
    assert result["terminal_data_proof"]["verified"] is True
    assert result["population_600_year_passed"] is False


@pytest.mark.parametrize("failure", ["manifest_mismatch", "active_job"])
def test_terminal_read_only_proof_rejects_mismatch_or_active_job(tmp_path, failure):
    data_directory, prefix, report, manifest_path = _terminal_probe_fixture(
        tmp_path, finish_terminal_job=failure != "active_job",
    )
    if failure == "manifest_mismatch":
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["last_state_hash"] = "f" * 64
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    result = merge.merge_prefix_and_intervals(prefix, [report])
    expected = "succeeded at its exact target" if failure == "active_job" else "final receipt"
    with pytest.raises(merge.MergeError, match=expected):
        merge._attach_terminal_data_proof(result, prefix, data_directory)


def test_terminal_running_manifest_without_exact_target_checkpoint_is_rejected(tmp_path):
    data_directory, prefix, report, _manifest_path = _terminal_probe_fixture(
        tmp_path, manifest_status="running", include_target_checkpoint=False,
    )

    result = merge.merge_prefix_and_intervals(prefix, [report])
    with pytest.raises(merge.MergeError, match="no unique exact target checkpoint"):
        merge._attach_terminal_data_proof(result, prefix, data_directory)


@pytest.mark.parametrize("protected_name", [
    ".probe-world-advance.json", "local-identity.json", "world-engine.db",
])
def test_terminal_cli_refuses_to_overwrite_read_only_data_sources(tmp_path, monkeypatch, protected_name):
    data_directory, prefix, report, _manifest_path = _terminal_probe_fixture(tmp_path)
    prefix_path, report_path = tmp_path / "prefix.json", tmp_path / "interval.json"
    prefix_path.write_text(json.dumps(prefix), encoding="utf-8")
    report_path.write_text(json.dumps(report), encoding="utf-8")
    destination = data_directory / protected_name
    before = destination.read_bytes()
    monkeypatch.setattr(sys, "argv", [
        str(SCRIPT), "--genesis-prefix", str(prefix_path), str(report_path),
        "--verify-terminal-data-directory", str(data_directory), "--output", str(destination),
    ])

    with pytest.raises(SystemExit) as error:
        merge.main()

    assert error.value.code == 2
    assert destination.read_bytes() == before


def test_terminal_cli_refuses_to_overwrite_input_report_through_hard_link(tmp_path, monkeypatch):
    data_directory, prefix, report, _manifest_path = _terminal_probe_fixture(tmp_path)
    prefix_path, report_path = tmp_path / "prefix.json", tmp_path / "interval.json"
    prefix_path.write_text(json.dumps(prefix), encoding="utf-8")
    report_path.write_text(json.dumps(report), encoding="utf-8")
    output_alias = tmp_path / "interval-output-alias.json"
    try:
        output_alias.hardlink_to(report_path)
    except (AttributeError, OSError):
        return
    before = report_path.read_bytes()
    monkeypatch.setattr(sys, "argv", [
        str(SCRIPT), "--genesis-prefix", str(prefix_path), str(report_path),
        "--verify-terminal-data-directory", str(data_directory), "--output", str(output_alias),
    ])

    with pytest.raises(SystemExit) as error:
        merge.main()

    assert error.value.code == 2
    assert report_path.read_bytes() == before
    assert output_alias.read_bytes() == before

