"""Verify real DeepSeek intent routing and monthly work in an isolated save."""

import argparse
import json
import os
import sqlite3
import sys
from contextlib import closing
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from tempfile import mkdtemp
from uuid import UUID, uuid4

from fastapi.testclient import TestClient

from world_engine.application.operations import OperationsStore
from world_engine.application.simulation_jobs import SimulationJobs
from world_engine.domain.models import AccessScope, JobStatus, NewWorld
from world_engine.runtime import build_application


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests" / "api"))
from test_work_chat_contract import DAY, EMPLOYER_ACCOUNT_ID, _post_message, _with_work_routine, _world


def isolated_app(source: Path, directory: Path):
    identity_document = (source / "local-identity.json").read_text(encoding="utf-8")
    scope = AccessScope.model_validate_json(identity_document)
    (directory / "local-identity.json").write_text(identity_document, encoding="utf-8")
    with closing(sqlite3.connect((source / "operations.db").as_uri() + "?mode=ro", uri=True)) as db:
        providers = db.execute("SELECT document FROM providers WHERE tenant=? AND principal=? ORDER BY id",
                               OperationsStore.identity(scope)).fetchall()
    provider = next((config for (raw,) in providers if (config := json.loads(raw))["kind"] == "deepseek"
                     and config["base_url"].rstrip("/") == "https://api.deepseek.com" and config.get("enabled")), None)
    if provider is None or not provider.get("secret_ref"):
        raise SystemExit("Configure the official DeepSeek connection and credential reference first.")
    settings = OperationsStore(directory / "operations.db")
    provider_id = provider["provider_id"]
    settings.save_provider(scope, provider, provider_id)
    settings.save_settings(scope, {
        "speed": "balanced", "depth": "deep", "local_only": False,
        "routes": {role: {"provider_id": provider_id, "model": "deepseek-flash"} for role in ("intent", "gm_answer")},
    })
    os.environ["WORLD_ENGINE_DATABASE_URL"] = f"sqlite:///{(directory / 'world-engine.db').as_posix()}"
    return build_application(data_directory=directory, run_simulation_worker=False), scope, settings


def run_probe(client: TestClient, repo, scope) -> dict:
    state, _ = _world()
    state = _with_work_routine(state)
    payer = state.resources[EMPLOYER_ACCOUNT_ID].model_copy(update={"balance": Decimal("500")})
    state = state.model_copy(update={"resources": state.resources | {EMPLOYER_ACCOUNT_ID: payer}})
    created = repo.create_world(scope, NewWorld(title="月度出勤实测", state=state))
    world, branch = created.world.world_id, created.branch.branch_id
    baseline = repo.read_world(scope, world, branch)
    client.get("/api/v1/session").raise_for_status()

    answer = _post_message(client, created, request_id=uuid4(), mode="auto",
                           text="如果我照常上班一个月，会攒下多少银币？先不要安排。").raise_for_status().json()
    assert answer["kind"] in {"hypothetical", "query"} and not answer["world_effect"]
    assert not repo.list_jobs(scope, world, branch)
    assert repo.read_world(scope, world, branch).state_hash == baseline.state_hash

    queued = _post_message(client, created, request_id=uuid4(), mode="auto",
                           text="现在安排我按已保存的排班照常上班一个月，工资存起来，不买东西。").raise_for_status().json()
    job_id = UUID(queued["job_id"])
    assert repo.read_job(scope, job_id).payload["plan"]["target_tick"] == 30 * DAY
    assert repo.read_world(scope, world, branch).state_hash == baseline.state_hash
    lease = repo.claim_job("real-model-work-probe")
    assert lease is not None and lease.job_id == job_id
    jobs = SimulationJobs(repo)
    for _ in range(100):
        jobs.advance(scope, lease)
        job = repo.read_job(scope, job_id)
        if job.status == JobStatus.SUCCEEDED:
            break
        assert job.status == JobStatus.RUNNING, job.status
    else:
        raise AssertionError("The monthly work probe did not reach the authorized deadline.")
    final = repo.read_world(scope, world, branch)
    progress = job.payload["work_progress"]
    assert final.state.current_tick == 30 * DAY and progress["shifts_paid"] == 30
    assert repo.replay(scope, world, branch, from_genesis=True).state_hash == final.state_hash
    return {"hypothetical_kind": answer["kind"], "hypothetical_time_advanced": False,
            "elapsed_world_seconds": final.state.current_tick, "paid_shifts": progress["shifts_paid"],
            "wages_by_resource": progress["wages_by_resource"], "replay_matches": True}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-data", type=Path, default=ROOT / ".data",
                        help="Existing local identity and provider settings, opened read-only.")
    args = parser.parse_args()
    runtime = ROOT / ".runtime"
    runtime.mkdir(exist_ok=True)
    directory = Path(mkdtemp(prefix="real-work-probe-", dir=runtime))
    app, scope, settings = isolated_app(args.source_data.resolve(), directory)
    with TestClient(app, base_url="http://127.0.0.1:8000") as client:
        evidence = run_probe(client, app.state.worlds.repository, scope)
    calls = list(reversed(settings.list_calls(scope)))
    assert calls and all(call["requested_model"] == call["reported_model"] == "deepseek-flash" for call in calls)
    evidence["calls"] = [
        {key: call[key] for key in ("role_task", "requested_model", "reported_model", "usage", "cost")} | {
            "elapsed_seconds": (datetime.fromisoformat(call["ended_at"]) - datetime.fromisoformat(call["started_at"])).total_seconds(),
        } for call in calls
    ]
    output = directory / "evidence.json"
    output.write_text(json.dumps(evidence, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(output.relative_to(ROOT), flush=True)


if __name__ == "__main__":
    main()
