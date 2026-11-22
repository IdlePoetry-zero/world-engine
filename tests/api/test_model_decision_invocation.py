from __future__ import annotations

import asyncio
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
import importlib.util
from pathlib import Path
from types import SimpleNamespace
import sys
from uuid import uuid4

import pytest

from world_engine.adapters.models.contracts import (
    ChatMessage,
    DataPolicy,
    DepthProfile,
    FinishReason,
    ModelRequest,
    RoleTask,
    SpeedProfile,
)
from world_engine.adapters.models.errors import ProviderError, ProviderErrorCode
from world_engine.application import model_decisions
from world_engine.application.model_decisions import (
    InvalidModelDecisionOutput,
    MODEL_DECISION_FINAL_OUTPUT_TOKENS,
    MODEL_DECISION_INITIAL_OUTPUT_TOKENS,
    MODEL_DECISION_LENGTH_RETRY_OUTPUT_TOKENS,
    invoke_model_decision,
)
from world_engine.application.operations import OperationsStore
from world_engine.application.simulation_jobs import SimulationJobs
from world_engine.domain.models import (
    AccessScope,
    JobLease,
    JobRecord,
    JobStatus,
    NewWorld,
    WorldEntity,
    WorldState,
    utc_now,
)
from world_engine.simulation.contracts import DecisionOption, DecisionRequest
from world_engine.simulation.contracts import WorldAdvancePlan
from world_engine.storage.database import Database
from world_engine.storage.repository import WorldRepository


def _load_probe_runner():
    script = Path(__file__).resolve().parents[2] / "scripts" / "probe-world-advance.py"
    spec = importlib.util.spec_from_file_location("probe_world_advance_test_module", script)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_runner_decision_mode_is_opt_in_and_default_hash_inputs_are_unchanged(tmp_path):
    runner = _load_probe_runner()
    parser = runner._build_parser(["fixture-pack"])
    default_args = parser.parse_args([
        "--pack", "fixture-pack", "--seed", "22", "--years", "1",
        "--data-directory", str(tmp_path / "probe"),
    ])
    assert default_args.decision_mode == "deterministic_no_model"
    assert default_args.resume_model_failure is False
    assert default_args.model_data_directory == runner.PROJECT_ROOT / ".data"
    assert runner._resume_model_failure_enabled(default_args) is False

    baseline = runner._run_config(
        default_args, "1.7.0", year_days=365, day_seconds=86_400, catalog_seeded=True
    )
    explicit_default_args = parser.parse_args([
        "--pack", "fixture-pack", "--seed", "22", "--years", "1",
        "--data-directory", str(tmp_path / "probe"),
        "--decision-mode", "deterministic_no_model",
    ])
    assert runner._run_config(
        explicit_default_args, "1.7.0", year_days=365, day_seconds=86_400, catalog_seeded=True
    ) == baseline

    configured_args = parser.parse_args([
        "--pack", "fixture-pack", "--seed", "22", "--years", "1",
        "--data-directory", str(tmp_path / "probe"),
        "--decision-mode", "configured_local_model",
    ])
    configured = runner._run_config(
        configured_args, "1.7.0", year_days=365, day_seconds=86_400, catalog_seeded=True
    )
    assert configured["decision_mode"] == "configured_local_model"
    assert {key: value for key, value in baseline.items() if key != "decision_mode"} == {
        key: value for key, value in configured.items() if key != "decision_mode"
    }
    configured_resume_args = parser.parse_args([
        "--pack", "fixture-pack", "--seed", "22", "--years", "1",
        "--data-directory", str(tmp_path / "probe"),
        "--decision-mode", "configured_local_model", "--resume-model-failure",
    ])
    assert runner._resume_model_failure_enabled(configured_resume_args) is True
    assert runner._run_config(
        configured_resume_args, "1.7.0", year_days=365, day_seconds=86_400, catalog_seeded=True
    ) == configured
    invalid_resume_args = parser.parse_args([
        "--pack", "fixture-pack", "--seed", "22", "--years", "1",
        "--data-directory", str(tmp_path / "probe"), "--resume-model-failure",
    ])
    with pytest.raises(runner.ProbeError, match="requires --decision-mode configured_local_model"):
        runner._resume_model_failure_enabled(invalid_resume_args)

    legacy_manifest = {"config": {
        "pack_id": "fixture-pack", "seed": 22, "declared_years": 1, "declared_days": 0,
    }}
    assert runner._legacy_config_for_resume(legacy_manifest, default_args) is not None
    with pytest.raises(runner.ProbeError, match="cannot change its decision mode"):
        runner._legacy_config_for_resume(legacy_manifest, configured_args)


def test_configured_model_profile_uses_only_local_operations_metering(tmp_path, monkeypatch):
    runner = _load_probe_runner()
    model_directory = tmp_path / "model-profile"
    model_directory.mkdir()
    model_scope = AccessScope(tenant_id=uuid4(), user_id=uuid4())
    (model_directory / runner.IDENTITY_NAME).write_text(
        model_scope.model_dump_json(), encoding="utf-8"
    )
    store = OperationsStore(model_directory / "operations.db")
    provider_id = "fixture-local"
    store.save_provider(model_scope, {
        "kind": "ollama",
        "name": "Local fixture",
        "base_url": "http://127.0.0.1:11434",
        "default_model": "fixture-model",
        "secret_ref": None,
        "enabled": True,
    }, provider_id)
    store.save_settings(model_scope, {
        "speed": "balanced",
        "depth": "deep",
        "local_only": True,
        "routes": {RoleTask.NPC_DECISION.value: {"provider_id": provider_id, "model": "fixture-model"}},
    })

    @dataclass(frozen=True)
    class FakeTrace:
        request_id = "metered-fixture-call"
        started_at: datetime = datetime.now(timezone.utc)
        queue_seconds: float | None = None

        def to_public_dict(self):
            return {
                "request_id": self.request_id,
                "role_task": "npc_decision",
                "queue_seconds": self.queue_seconds,
            }

    @dataclass(frozen=True)
    class FakeResult:
        normalized_output: dict[str, str]
        call_trace: FakeTrace

    class FakeGateway:
        async def generate(self, config, request):
            assert config.provider_id == provider_id
            assert config.secret_ref is None
            assert request.data_policy is DataPolicy.LOCAL_ONLY
            return FakeResult(normalized_output={"option_id": "leave_vacant"}, call_trace=FakeTrace())

    monkeypatch.setattr(runner, "ModelGateway", lambda **_kwargs: FakeGateway())
    operations, resolved_scope = runner._configured_model_operations(model_directory)
    result = asyncio.run(operations.generate(
        resolved_scope,
        ModelRequest(
            role_task=RoleTask.NPC_DECISION,
            messages=(ChatMessage("user", "choose an offered option"),),
        ),
    ))

    assert resolved_scope == model_scope
    assert result.normalized_output == {"option_id": "leave_vacant"}
    assert operations.store.path == model_directory / "operations.db"
    assert operations.store.list_calls(model_scope)[0]["request_id"] == "metered-fixture-call"
    assert not (model_directory / "world-engine.db").exists()


def _call_context():
    scope = AccessScope(tenant_id=uuid4(), user_id=uuid4())
    now = utc_now()
    lease = JobLease(
        job_id=uuid4(),
        world_id=uuid4(),
        branch_id=uuid4(),
        worker_id="model-decision-test",
        fencing_token=1,
        expires_at=now + timedelta(seconds=120),
    )
    job = JobRecord(
        job_id=lease.job_id,
        tenant_id=scope.tenant_id,
        world_id=lease.world_id,
        branch_id=lease.branch_id,
        job_type="simulation.world_advance",
        status=JobStatus.RUNNING,
        payload={"speed": "patient", "depth": "exhaustive"},
        attempt_count=1,
        fencing_token=1,
        created_at=now,
        updated_at=now,
    )
    decision = DecisionRequest(
        request_id=uuid4(),
        kind="organization.succession",
        decision_owner="model",
        at_tick=120,
        state_version=9,
        title="Select a successor",
        reason_code="succession_required",
        options=(
            DecisionOption(option_id="leader_candidate_1", label="Appoint Rowan"),
            DecisionOption(option_id="leave_vacant", label="Leave vacant"),
        ),
    )
    return scope, lease, job, decision


@pytest.mark.asyncio
async def test_invocation_uses_typed_allowlist_profile_trace_and_bound_response():
    class RecordingOperations:
        request = None
        seen_scope = None

        async def generate(self, scope, request):
            self.seen_scope = scope
            self.request = request
            return SimpleNamespace(normalized_output={"option_id": "leave_vacant"})

    operations = RecordingOperations()
    scope, lease, job, decision = _call_context()

    response = await invoke_model_decision(operations, scope, lease, job, decision)

    assert operations.seen_scope == scope
    assert operations.request.role_task is RoleTask.NPC_DECISION
    assert operations.request.profile.speed is SpeedProfile.PATIENT
    assert operations.request.profile.depth is DepthProfile.EXHAUSTIVE
    assert operations.request.output_schema["properties"]["option_id"]["enum"] == [
        "leader_candidate_1", "leave_vacant",
    ]
    assert operations.request.trace_context.job_id == str(lease.job_id)
    assert operations.request.trace_context.parent_trace == str(decision.request_id)
    assert operations.request.trace_context.world_id == str(lease.world_id)
    assert response.request_id == decision.request_id
    assert response.state_version == decision.state_version
    assert response.at_tick == decision.at_tick
    assert response.option_id == "leave_vacant"
    assert response.source == "model"


@pytest.mark.asyncio
async def test_invocation_retries_twice_only_after_explicit_output_length_finish():
    class TruncatesTwiceThenSucceeds:
        requests = []

        async def generate(self, scope, request):
            self.requests.append((scope, request))
            if len(self.requests) <= 2:
                error = ProviderError(ProviderErrorCode.TRUNCATED, "bounded test error")
                error.call_trace = SimpleNamespace(
                    finish_reason=FinishReason.LENGTH,
                    max_output_tokens=request.profile.max_output_tokens,
                )
                raise error
            return SimpleNamespace(normalized_output={"option_id": "leave_vacant"})

    operations = TruncatesTwiceThenSucceeds()
    scope, lease, job, decision = _call_context()
    response = await invoke_model_decision(operations, scope, lease, job, decision)

    assert len(operations.requests) == 3
    assert all(request_scope == scope for request_scope, _ in operations.requests)
    requests = [request for _, request in operations.requests]
    first_request, second_request, third_request = requests
    assert first_request.profile.max_output_tokens == MODEL_DECISION_INITIAL_OUTPUT_TOKENS == 2048
    assert second_request.profile.max_output_tokens == MODEL_DECISION_LENGTH_RETRY_OUTPUT_TOKENS == 4096
    assert third_request.profile.max_output_tokens == MODEL_DECISION_FINAL_OUTPUT_TOKENS == 8192
    assert len({request.trace_context.request_id for request in requests}) == 3
    for request in requests:
        assert replace(
            request,
            profile=first_request.profile,
            trace_context=first_request.trace_context,
        ) == first_request
        assert request.trace_context.parent_trace == str(decision.request_id)
        assert request.trace_context.world_id == str(lease.world_id)
        assert request.trace_context.branch_id == str(lease.branch_id)
        assert request.trace_context.job_id == str(lease.job_id)
        assert request.profile.speed is first_request.profile.speed
        assert request.profile.depth is first_request.profile.depth
    assert response.option_id == "leave_vacant"


@pytest.mark.asyncio
async def test_invocation_stops_after_final_explicit_length_truncation():
    class AlwaysTruncates:
        requests = []

        async def generate(self, scope, request):
            self.requests.append(request)
            error = ProviderError(ProviderErrorCode.TRUNCATED, "bounded test error")
            error.call_trace = SimpleNamespace(
                finish_reason=FinishReason.LENGTH,
                max_output_tokens=request.profile.max_output_tokens,
            )
            raise error

    operations = AlwaysTruncates()
    scope, lease, job, decision = _call_context()
    with pytest.raises(ProviderError):
        await invoke_model_decision(operations, scope, lease, job, decision)

    assert [request.profile.max_output_tokens for request in operations.requests] == [
        MODEL_DECISION_INITIAL_OUTPUT_TOKENS,
        MODEL_DECISION_LENGTH_RETRY_OUTPUT_TOKENS,
        MODEL_DECISION_FINAL_OUTPUT_TOKENS,
    ]
    assert len({request.trace_context.request_id for request in operations.requests}) == 3


@pytest.mark.asyncio
async def test_invocation_stops_when_trace_reports_a_route_overridden_budget():
    class RouteOverridesRequestedBudget:
        calls = 0

        async def generate(self, scope, request):
            self.calls += 1
            error = ProviderError(ProviderErrorCode.TRUNCATED, "bounded test error")
            error.call_trace = SimpleNamespace(
                finish_reason=FinishReason.LENGTH,
                max_output_tokens=MODEL_DECISION_LENGTH_RETRY_OUTPUT_TOKENS,
            )
            raise error

    operations = RouteOverridesRequestedBudget()
    scope, lease, job, decision = _call_context()
    with pytest.raises(ProviderError):
        await invoke_model_decision(operations, scope, lease, job, decision)
    assert operations.calls == 1


@pytest.mark.parametrize(
    ("code", "finish_reason"),
    [
        (ProviderErrorCode.TRUNCATED, None),
        (ProviderErrorCode.TRUNCATED, FinishReason.OTHER),
        (ProviderErrorCode.PROTOCOL, FinishReason.LENGTH),
        (ProviderErrorCode.INVALID_OUTPUT, FinishReason.LENGTH),
        (ProviderErrorCode.NETWORK, FinishReason.LENGTH),
    ],
)
@pytest.mark.asyncio
async def test_invocation_does_not_retry_without_exact_length_truncation_proof(code, finish_reason):
    class FailsWithoutLengthProof:
        calls = 0

        async def generate(self, scope, request):
            self.calls += 1
            error = ProviderError(code, "provider failure")
            if finish_reason is not None:
                error.call_trace = SimpleNamespace(finish_reason=finish_reason)
            raise error

    operations = FailsWithoutLengthProof()
    scope, lease, job, decision = _call_context()
    with pytest.raises(ProviderError):
        await invoke_model_decision(operations, scope, lease, job, decision)
    assert operations.calls == 1


@pytest.mark.asyncio
async def test_invocation_retry_remains_inside_one_overall_deadline(monkeypatch):
    class DeterministicDeadline:
        def __init__(self, timeout_seconds):
            self.timeout_seconds = timeout_seconds
            self.elapsed_seconds = 0

        async def __aenter__(self):
            return self

        async def __aexit__(self, _exception_type, _exception, _traceback):
            return False

        def advance(self, seconds):
            self.elapsed_seconds += seconds
            if self.elapsed_seconds > self.timeout_seconds:
                raise TimeoutError

    class TimeoutFactory:
        def __init__(self):
            self.contexts = []

        def __call__(self, timeout_seconds):
            context = DeterministicDeadline(timeout_seconds)
            self.contexts.append(context)
            return context

    # Use a deterministic clock for the retry-loop contract; the separate
    # test_invocation_deadline_is_enforced covers asyncio's real timeout.
    timeout_factory = TimeoutFactory()
    monkeypatch.setattr(model_decisions, "asyncio", SimpleNamespace(timeout=timeout_factory))

    class ExpiresDuringRetry:
        calls = 0

        async def generate(self, scope, request):
            self.calls += 1
            if self.calls == 1:
                timeout_factory.contexts[0].advance(6)
                error = ProviderError(ProviderErrorCode.TRUNCATED, "length-limited")
                error.call_trace = SimpleNamespace(finish_reason=FinishReason.LENGTH)
                raise error
            timeout_factory.contexts[0].advance(5)
            return SimpleNamespace(normalized_output={"option_id": "leave_vacant"})

    operations = ExpiresDuringRetry()
    scope, lease, job, decision = _call_context()
    with pytest.raises(TimeoutError):
        await invoke_model_decision(operations, scope, lease, job, decision, timeout_seconds=10)
    assert operations.calls == 2
    assert len(timeout_factory.contexts) == 1
    assert timeout_factory.contexts[0].timeout_seconds == 10
    assert timeout_factory.contexts[0].elapsed_seconds == 11


@pytest.mark.asyncio
async def test_invocation_does_not_retry_cancellation():
    class CancelledCall:
        calls = 0
        started = asyncio.Event()

        async def generate(self, scope, request):
            self.calls += 1
            self.started.set()
            await asyncio.Future()

    operations = CancelledCall()
    scope, lease, job, decision = _call_context()
    task = asyncio.create_task(invoke_model_decision(operations, scope, lease, job, decision))
    await operations.started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert operations.calls == 1


@pytest.mark.asyncio
async def test_invocation_validates_retry_output_and_does_not_invent_fallback():
    class InvalidRetryOutput:
        calls = 0

        async def generate(self, scope, request):
            self.calls += 1
            if self.calls == 1:
                error = ProviderError(ProviderErrorCode.TRUNCATED, "length-limited")
                error.call_trace = SimpleNamespace(finish_reason=FinishReason.LENGTH)
                raise error
            return SimpleNamespace(normalized_output={"option_id": "not_offered"})

    operations = InvalidRetryOutput()
    scope, lease, job, decision = _call_context()
    with pytest.raises(InvalidModelDecisionOutput, match="outside the allowed set"):
        await invoke_model_decision(operations, scope, lease, job, decision)
    assert operations.calls == 2


@pytest.mark.asyncio
async def test_invocation_rejects_model_option_outside_allowlist():
    class InvalidOperations:
        async def generate(self, scope, request):
            return SimpleNamespace(normalized_output={"option_id": "unlisted_person"})

    scope, lease, job, decision = _call_context()
    with pytest.raises(InvalidModelDecisionOutput, match="outside the allowed set"):
        await invoke_model_decision(InvalidOperations(), scope, lease, job, decision)


@pytest.mark.asyncio
async def test_invocation_refuses_user_owned_decisions():
    class UnexpectedOperations:
        async def generate(self, scope, request):
            raise AssertionError("user-owned decisions must not reach a model")

    scope, lease, job, decision = _call_context()
    user_decision = decision.model_copy(update={"decision_owner": "user"})
    with pytest.raises(InvalidModelDecisionOutput, match="not model-owned"):
        await invoke_model_decision(UnexpectedOperations(), scope, lease, job, user_decision)


@pytest.mark.asyncio
async def test_invocation_deadline_is_enforced():
    class SlowOperations:
        async def generate(self, scope, request):
            await asyncio.sleep(0.05)
            return SimpleNamespace(normalized_output={"option_id": "leave_vacant"})

    scope, lease, job, decision = _call_context()
    with pytest.raises(TimeoutError):
        await invoke_model_decision(
            SlowOperations(), scope, lease, job, decision, timeout_seconds=0.005
        )


@pytest.mark.parametrize(
    ("player_is_voter", "model_failure", "transient_failures"),
    [
        (False, None, 0),
        (True, None, 0),
        (False, "runtime", 0),
        (False, "maintenance_before_model", 0),
        (False, "maintenance_during_model", 0),
        (
            False,
            ProviderError(
                ProviderErrorCode.NETWORK,
                "offline",
                retryable=True,
                possibly_billed=False,
            ),
            2,
        ),
        (
            False,
            ProviderError(
                ProviderErrorCode.NETWORK,
                "still offline",
                retryable=True,
                possibly_billed=False,
            ),
            3,
        ),
        (
            False,
            ProviderError(
                ProviderErrorCode.NETWORK,
                "ambiguous network failure",
                retryable=True,
                possibly_billed=None,
            ),
            1,
        ),
        (
            False,
            ProviderError(
                ProviderErrorCode.NETWORK,
                "possibly billed network failure",
                retryable=True,
                possibly_billed=True,
            ),
            1,
        ),
        (
            False,
            ProviderError(
                ProviderErrorCode.NETWORK,
                "non-retryable network failure",
                retryable=False,
                possibly_billed=False,
            ),
            1,
        ),
        (
            False,
            ProviderError(
                ProviderErrorCode.TIMEOUT,
                "provider timeout",
                retryable=True,
                possibly_billed=False,
            ),
            1,
        ),
    ],
)
def test_capacity_runner_resolves_only_model_owned_decisions_and_keeps_lease(
    tmp_path, monkeypatch, player_is_voter, model_failure, transient_failures,
):
    runner = _load_probe_runner()
    scope = AccessScope(tenant_id=uuid4(), user_id=uuid4())
    player, leader, successor, council, leader_two, successor_two, council_two = (
        uuid4() for _ in range(7)
    )
    members = [leader, successor]
    if player_is_voter:
        members.append(player)
    state = WorldState(
        player_id=player,
        entities={
            player: WorldEntity(id=player, kind="character", name="Player"),
            leader: WorldEntity(
                id=leader,
                kind="character",
                name="Departing leader",
                components={"life": {"birth_tick": 0, "lifespan_ticks": 60}},
            ),
            successor: WorldEntity(id=successor, kind="character", name="Eligible successor"),
            leader_two: WorldEntity(
                id=leader_two,
                kind="character",
                name="Second departing leader",
                components={"life": {"birth_tick": 0, "lifespan_ticks": 60}},
            ),
            successor_two: WorldEntity(
                id=successor_two, kind="character", name="Second eligible successor"
            ),
            council: WorldEntity(
                id=council,
                kind="organization",
                name="Town council",
                components={
                    "governance": {
                        "leader_id": str(leader),
                        "members": [str(item) for item in members],
                        "succession_policy": "election",
                    }
                },
            ),
            council_two: WorldEntity(
                id=council_two,
                kind="organization",
                name="Second town council",
                components={
                    "governance": {
                        "leader_id": str(leader_two),
                        "members": [
                            str(leader_two), str(successor_two),
                            *([str(player)] if player_is_voter else []),
                        ],
                        "succession_policy": "election",
                    }
                },
            ),
        },
    )
    database = Database(f"sqlite:///{(tmp_path / f'runner-{player_is_voter}.db').as_posix()}")
    database.create_schema()
    try:
        repository = WorldRepository(database)
        world = repository.create_world(scope, NewWorld(title="Runner decision", state=state))
        world_id, branch_id = world.world.world_id, world.branch.branch_id
        jobs = SimulationJobs(repository)
        job = jobs.enqueue_world_advance(
            scope,
            world_id,
            branch_id,
            WorldAdvancePlan(target_tick=120, started_tick=0),
            idempotency_key=f"runner-decision-{player_is_voter}",
            speed="balanced",
            depth="deep",
            expected_state_version=0,
        )

        def request_maintenance():
            runner._atomic_json(tmp_path / runner.MAINTENANCE_REQUEST_NAME, {"schema": 1, "job_id": str(job.job_id)})

        if model_failure == "maintenance_before_model":
            original_advance = jobs.advance

            def advance_then_request(*args, **kwargs):
                resolution = original_advance(*args, **kwargs)
                if getattr(resolution, "outcome", None) == "decision_required":
                    request_maintenance()
                return resolution

            jobs.advance = advance_then_request

        class FakeModelOperations:
            calls = 0
            requests = []

            async def generate(self, model_scope, request):
                self.calls += 1
                self.requests.append(request)
                assert model_scope == scope
                assert request.role_task is RoleTask.NPC_DECISION
                if model_failure == "maintenance_during_model":
                    request_maintenance()
                if model_failure == "runtime":
                    raise RuntimeError("TEST_SECRET_MUST_NOT_LEAK")
                if isinstance(model_failure, ProviderError) and self.calls <= transient_failures:
                    raise model_failure
                return SimpleNamespace(normalized_output={"option_id": "leave_vacant"})

        operations = FakeModelOperations()
        backoffs = []
        monkeypatch.setattr(runner.time, "sleep", backoffs.append)
        renewals = []
        original_renew = repository.renew_job

        def counting_renew(lease, *, lease_seconds):
            renewals.append(lease.fencing_token)
            return original_renew(lease, lease_seconds=lease_seconds)

        repository.renew_job = counting_renew
        manifest = {"checkpoints": []}
        result, outcomes, stop_reasons, _windows = runner._advance_job(
            repository=repository,
            scope=scope,
            jobs=jobs,
            job=job,
            world_id=world_id,
            branch_id=branch_id,
            start_tick=0,
            target_tick=120,
            day_seconds=86_400,
            year_days=365,
            max_windows=10,
            lease_wait_seconds=0,
            manifest=manifest,
            manifest_path=tmp_path / "manifest.json",
            decision_mode="configured_local_model",
            model_operations=operations,
            model_scope=scope,
        )

        retryable_network = (
            isinstance(model_failure, ProviderError)
            and model_failure.code is ProviderErrorCode.NETWORK
            and model_failure.retryable is True
            and model_failure.possibly_billed is False
        )
        model_call_failed = model_failure == "runtime" or (
            isinstance(model_failure, ProviderError)
            and (not retryable_network or transient_failures > 2)
        )

        if model_failure in ("maintenance_before_model", "maintenance_during_model"):
            expected_calls = int(model_failure == "maintenance_during_model")
            assert operations.calls == expected_calls
            assert result.status is JobStatus.PAUSED
            assert (result.result or {}).get("probe_pause") == runner.MAINTENANCE_PAUSE
            assert stop_reasons[-1] == runner.MAINTENANCE_PAUSE
            assert outcomes.get("model_decisions_accepted", 0) == expected_calls
            assert "pending_decision" not in result.payload
            assert ("last_decision" in result.payload) is bool(expected_calls)
        elif player_is_voter:
            assert operations.calls == 0
            assert result.status is JobStatus.WAITING_INPUT
            assert "user_decision_required" in stop_reasons
        elif model_call_failed:
            expected_failed_calls = transient_failures if retryable_network else 1
            assert operations.calls == expected_failed_calls
            assert result.status is JobStatus.PAUSED
            assert (result.result or {}).get("probe_pause") == "model_decision_failed"
            assert outcomes["model_call_failures"] == 1
            assert "model_decision_failed" in stop_reasons
            assert "TEST_SECRET_MUST_NOT_LEAK" not in repr(result.result)
            expected_retries = max(0, expected_failed_calls - 1) if retryable_network else 0
            assert outcomes.get("model_retry_attempts", 0) == expected_retries
            assert backoffs == [2, 4][:expected_retries]
            if retryable_network:
                assert outcomes["model_unbilled_network_failures"] == transient_failures
                for attempt_number in range(1, expected_failed_calls + 1):
                    assert outcomes[f"model_call_attempt_{attempt_number}"] == 1
            else:
                assert outcomes.get("model_unbilled_network_failures", 0) == 0
            assert len(renewals) >= operations.calls + 1
        else:
            expected_calls = 2 + transient_failures
            assert operations.calls == expected_calls
            assert result.status is JobStatus.SUCCEEDED
            assert result.committed_through_tick == 120
            assert outcomes["model_calls_attempted"] == expected_calls
            assert outcomes["model_decisions_accepted"] == 2
            assert outcomes.get("model_retry_attempts", 0) == transient_failures
            assert backoffs == [2, 4][:transient_failures]
            if transient_failures:
                assert outcomes["model_call_attempt_1"] == 2
                assert outcomes["model_call_attempt_2"] == 1
                assert outcomes["model_call_attempt_3"] == 1
                attempted_requests = operations.requests[: transient_failures + 1]
                first_request = attempted_requests[0]
                assert all(request.messages == first_request.messages for request in attempted_requests)
                assert all(request.output_schema == first_request.output_schema for request in attempted_requests)
                assert all(
                    request.trace_context.parent_trace == first_request.trace_context.parent_trace
                    for request in attempted_requests
                )
            assert len(renewals) >= operations.calls + 1
            view = repository.read_world(scope, world_id, branch_id)
            assert view.state.current_tick == 120
            assert view.state.entities[leader].status == "dead"
            assert view.state.entities[leader_two].status == "dead"
            assert repository.replay(scope, world_id, branch_id).state_hash == view.state_hash
    finally:
        database.dispose()


def test_capacity_runner_resumes_model_failure_only_when_explicitly_requested(
    tmp_path, monkeypatch,
):
    runner = _load_probe_runner()
    scope = AccessScope(tenant_id=uuid4(), user_id=uuid4())
    player, leader, successor, council = (uuid4() for _ in range(4))
    state = WorldState(
        player_id=player,
        entities={
            player: WorldEntity(id=player, kind="character", name="Player"),
            leader: WorldEntity(
                id=leader,
                kind="character",
                name="Departing leader",
                components={"life": {"birth_tick": 0, "lifespan_ticks": 60}},
            ),
            successor: WorldEntity(
                id=successor, kind="character", name="Eligible successor"
            ),
            council: WorldEntity(
                id=council,
                kind="organization",
                name="Town council",
                components={
                    "governance": {
                        "leader_id": str(leader),
                        "members": [str(leader), str(successor)],
                        "succession_policy": "election",
                    }
                },
            ),
        },
    )
    database = Database(f"sqlite:///{(tmp_path / 'runner-resume-model-failure.db').as_posix()}")
    database.create_schema()
    try:
        repository = WorldRepository(database)
        world = repository.create_world(
            scope, NewWorld(title="Resume model failure", state=state)
        )
        world_id, branch_id = world.world.world_id, world.branch.branch_id
        jobs = SimulationJobs(repository)
        job = jobs.enqueue_world_advance(
            scope,
            world_id,
            branch_id,
            WorldAdvancePlan(target_tick=120, started_tick=0),
            idempotency_key="resume-model-failure",
            speed="balanced",
            depth="deep",
            expected_state_version=0,
        )

        class SwitchableModelOperations:
            calls = 0
            requests = []
            fail = True

            async def generate(self, model_scope, request):
                self.calls += 1
                self.requests.append(request)
                assert model_scope == scope
                assert request.role_task is RoleTask.NPC_DECISION
                if self.fail:
                    raise ProviderError(
                        ProviderErrorCode.NETWORK,
                        "possibly billed fixture failure",
                        retryable=True,
                        possibly_billed=True,
                    )
                return SimpleNamespace(normalized_output={"option_id": "leave_vacant"})

        operations = SwitchableModelOperations()
        monkeypatch.setattr(runner.time, "sleep", lambda _seconds: None)
        common = dict(
            repository=repository,
            scope=scope,
            jobs=jobs,
            job=job,
            world_id=world_id,
            branch_id=branch_id,
            start_tick=0,
            target_tick=120,
            day_seconds=86_400,
            year_days=365,
            max_windows=10,
            lease_wait_seconds=0,
            manifest={"checkpoints": []},
            manifest_path=tmp_path / "manifest.json",
            decision_mode="configured_local_model",
            model_operations=operations,
            model_scope=scope,
        )

        paused, first_outcomes, first_stops, _ = runner._advance_job(**common)
        assert operations.calls == 1
        assert first_outcomes["model_call_failures"] == 1
        assert first_outcomes.get("model_retry_attempts", 0) == 0
        assert "model_decision_failed" in first_stops
        assert paused.status is JobStatus.PAUSED
        assert (paused.result or {}).get("probe_pause") == "model_decision_failed"
        assert paused.committed_through_tick == 60
        initial_request = operations.requests[0]
        initial_request_id = initial_request.trace_context.parent_trace
        view_before_opt_in = repository.read_world(scope, world_id, branch_id)

        unchanged, unchanged_outcomes, unchanged_stops, unchanged_windows = runner._advance_job(
            **common
        )
        assert operations.calls == 1
        assert unchanged_outcomes == {}
        assert unchanged_stops == ["model_decision_failed"]
        assert unchanged_windows == 0
        assert unchanged.status is JobStatus.PAUSED
        assert unchanged.attempt_count == paused.attempt_count
        unchanged_view = repository.read_world(scope, world_id, branch_id)
        assert unchanged.committed_through_tick == view_before_opt_in.state.current_tick == 60
        assert unchanged_view.state_hash == view_before_opt_in.state_hash

        operations.fail = False
        resumed, resumed_outcomes, resumed_stops, _ = runner._advance_job(
            **common, resume_model_failure=True
        )
        assert resumed_outcomes["model_failure_pause_resumes"] == 1
        assert resumed_outcomes["model_decisions_accepted"] == 1
        assert operations.calls == 2
        assert operations.requests[1].trace_context.parent_trace == initial_request_id
        assert operations.requests[1].messages == initial_request.messages
        assert operations.requests[1].output_schema == initial_request.output_schema
        assert resumed.status is JobStatus.SUCCEEDED
        assert resumed.committed_through_tick == 120
        assert "model_decision_failed" not in resumed_stops
        final_view = repository.read_world(scope, world_id, branch_id)
        assert final_view.state.current_tick == 120
        assert repository.replay(scope, world_id, branch_id).state_hash == final_view.state_hash
    finally:
        database.dispose()
