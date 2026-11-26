"""Exercise real time advancement and replay in an isolated verification world."""

import argparse
from collections import Counter
from datetime import datetime
from decimal import Decimal
from contextlib import contextmanager
from functools import wraps
from hashlib import sha256
import json
import os
from pathlib import Path
import re
from tempfile import mkdtemp
from time import monotonic, perf_counter, sleep
from threading import Lock
from types import SimpleNamespace
from uuid import UUID, uuid4, uuid5
from urllib.parse import quote

from fastapi.testclient import TestClient
from world_engine.adapters.models.contracts import RoleTask
from world_engine.adapters.models.errors import ProviderError, ProviderErrorCode
from world_engine.application.operations import ModelOperations, OperationsStore
from world_engine.application.visibility import project_event
from world_engine.runtime import build_application, local_identity


BUILTIN_BACKGROUNDS = frozenset({"modern_city", "low_magic_feudal", "long_life_cultivation"})


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("opening")
    parser.add_argument("mode", choices=("world", "rest", "quiet", "intervention"))
    parser.add_argument("duration", type=int)
    parser.add_argument("--timeout", type=int, default=3600)
    parser.add_argument("--archive", default=".runtime/world-packs/gensokyo-world-1.5.0.zip")
    parser.add_argument("--seed", type=int, default=19, help="Seed for a built-in background.")
    parser.add_argument("--builtin-version", default="2.13.0", help="Catalog version for a built-in background.")
    parser.add_argument("--fork", action="store_true", help="Create a branch at the unmodified opening before advancing.")
    parser.add_argument("--resume", type=Path, help="Resume one unfinished rest or world job from its isolated evidence directory.")
    parser.add_argument("--from-genesis", action="store_true", help="Verify the entire terminal ledger, including all earlier checkpoints.")
    parser.add_argument("--provider-unavailable", action="store_true", help="Exercise the native deferral policy without any model responses.")
    parser.add_argument("--cloud", action="store_true", help="Use the official DeepSeek connection exclusively.")
    parser.add_argument("--measure-storage", action="store_true", help="Measure engine phases in this isolated process.")
    args = parser.parse_args()
    if not re.fullmatch(r"[a-z][a-z0-9_-]{0,79}", args.opening):
        parser.error("Use a registered built-in background or story opening identifier.")
    chunk_count = {"quiet": 3, "intervention": 2}.get(args.mode, 1)
    if args.timeout < 1 or args.duration < chunk_count:
        parser.error("Choose a positive timeout and duration for every requested segment.")
    if args.mode == "intervention" and not args.cloud:
        parser.error("The intervention check exercises a real cloud dialogue.")
    if os.environ.get("WORLD_ENGINE_DATABASE_URL"):
        parser.error("An external database override cannot be used for an isolated verification run.")
    if args.cloud and (args.provider_unavailable or not os.environ.get("WORLD_ENGINE_DEEPSEEK_API_KEY")):
        parser.error("Cloud runs require a DeepSeek credential in the process and cannot inject provider failures.")
    evidence = Path(".runtime/autonomy-evidence")
    evidence.mkdir(parents=True, exist_ok=True)
    if args.resume is None:
        data = Path(mkdtemp(prefix=f"controlled-{args.opening}-{args.mode}-", dir=evidence))
        result = {"scope": "isolated official API, built-in worker, controlled data-driven planning responses; no model service",
                  "opening": args.opening, "mode": args.mode, "duration": args.duration, "database": str(data),
                  "provider_unavailable": args.provider_unavailable, "cloud": args.cloud,
                  "response_observations_complete": True}
    else:
        data = args.resume.resolve()
        if chunk_count != 1 or data.parent != evidence.resolve() or not data.name.startswith("controlled-"):
            parser.error("Resume accepts one isolated rest or world run inside .runtime/autonomy-evidence.")
        result = json.loads((data / "evidence.json").read_text(encoding="utf-8"))
        if (result.get("opening"), result.get("mode"), result.get("duration")) != (args.opening, args.mode, args.duration):
            parser.error("Use the opening, mode, and duration from the original run.")
        if "active_job" not in result:
            parser.error("The previous run did not submit a durable job.")
        if (result.get("provider_unavailable", False), result.get("cloud", False)) != (
            args.provider_unavailable, args.cloud,
        ):
            parser.error("Use the same provider mode as the original run.")
        result.setdefault("response_observations_complete", False)
    if args.provider_unavailable:
        result["scope"] = "isolated official API and built-in worker with injected model unavailability; no model service"
    elif args.cloud:
        result["scope"] = "isolated official API and built-in worker with actual DeepSeek responses; no local model"
    output = data / "evidence.json"
    origin = "http://127.0.0.1:39872"
    app = build_application(data_directory=data, port=39872, compact_ledger=True)
    scope = local_identity(data)
    if args.cloud:
        store = OperationsStore(data / "operations.db")
        provider = store.provider(scope, "deepseek")
        if (provider["kind"], provider["base_url"], provider["secret_ref"]) != (
            "deepseek", "https://api.deepseek.com", "env:WORLD_ENGINE_DEEPSEEK_API_KEY",
        ):
            parser.error("The isolated cloud run only accepts the bundled official DeepSeek environment binding.")
        store.save_settings(scope, {
            "speed": "fast", "depth": "standard", "local_only": False,
            "routes": {
                role.value: {"provider_id": "deepseek", "model": provider["default_model"]}
                for role in RoleTask if role is not RoleTask.EMBEDDING
            },
        })
    decisions = Counter(result.get("controlled_responses", {}))
    timings = _measure_storage_phases() if args.measure_storage else None

    async def generate(_self, _scope, request):
        data = json.loads(request.messages[-1].content)
        role = request.role_task
        decisions[str(role.value)] += 1
        if args.provider_unavailable:
            raise ProviderError(ProviderErrorCode.UNAVAILABLE, "Provider unavailable in isolated simulation verification.")
        if role is RoleTask.NPC_PLAN_REVIEW:
            return SimpleNamespace(normalized_output={"verdict": "approve"})
        if role is RoleTask.NPC_DECISION:
            if request.schema_ref == "ambient_event_proposal_v1":
                return SimpleNamespace(normalized_output={"decision": "abstain"})
            if "options" in data:
                options = data["options"]
                selected = next((item for item in options if "defer" in item["option_id"]), options[0])
                return SimpleNamespace(normalized_output={"option_id": selected["option_id"]})
            routes = {item["destination_id"]: item for item in data["known_routes"] if item["access_allowed_now"]}
            for goal in sorted(data["active_goals"], key=lambda item: -item["priority"]):
                for condition in goal["conditions"]:
                    route = routes.get(condition.get("target_id"))
                    if route is not None and condition["kind"] == "reach_location":
                        return SimpleNamespace(normalized_output={
                            "kind": "plan", "goal_ids": [goal["id"]], "reason": "Available route meets current goal.",
                            "steps": [{"capability_id": "world.action.move.v1", "arguments": {
                                "route_id": route["route_id"], "destination_id": route["destination_id"]}}]
                        })
                entities = data["known_view"]["entities"]
                actor = entities.get(data["actor"]["id"], {})
                peers = [
                    person for person in entities.values()
                    if person.get("id") != actor.get("id")
                    and person.get("kind") in ("character", "person")
                    and person.get("location_id") == actor.get("location_id")
                    and (person.get("id") == goal.get("counterpart_id") or (
                        person.get("name") and person["name"] in goal["title"]
                    ))
                ]
                replies = data.get("private_reply_options", [])
                if replies or peers:
                    if replies:
                        peer = replies[0]["speaker_id"]
                        speech = {"reply_to_memory_id": replies[0]["memory_id"],
                                  "utterance": "我听到了。我们可以先核对眼下知道的情况。"}
                    else:
                        peer = peers[0]["id"]
                        speech = {"utterance": "你现在方便谈谈这件事吗？" + goal["title"]}
                    return SimpleNamespace(normalized_output={
                        "kind": "plan", "goal_ids": [goal["id"]], "reason": "Available counterpart from the current scene.",
                        "steps": [{"capability_id": "world.action.speak.v1", "arguments": speech | {"target_ids": [peer]}}]
                    })
            return SimpleNamespace(normalized_output={"kind": "defer", "reason": "No eligible typed step from available observations."})
        if role is RoleTask.MEMORY_SUMMARY:
            return SimpleNamespace(normalized_output={"outlook": "defer"})
        raise AssertionError(role.value)

    if not args.cloud:
        ModelOperations.generate = generate

    def record(**fields):
        result.update(fields)
        pending = output.with_suffix(".tmp")
        pending.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        pending.replace(output)

    with TestClient(app, base_url=origin) as client:
        client.get("/api/v1/session").raise_for_status()
        repository = app.state.worlds.repository
        if args.resume is not None:
            world_id, branch_id, actor_id = (result[key] for key in ("world_id", "branch_id", "actor_id"))
            path = f"/api/v1/worlds/{world_id}"
            result["jobs"] = []
        else:
            archive_sha256 = None
            if args.opening in BUILTIN_BACKGROUNDS:
                world = client.post("/api/v1/worlds", headers={"Idempotency-Key": "builtin-strategy"}, json={
                    "title": "机构自治对照", "player_name": "旅人", "pack_id": args.opening,
                    "pack_version": args.builtin_version, "seed": args.seed,
                }).raise_for_status().json()
                version = args.builtin_version
            else:
                archive = Path(args.archive).read_bytes()
                headers = {"Content-Type": "application/zip", "Origin": origin}
                preview = client.post("/api/v1/packs/import/preview", headers=headers, content=archive).raise_for_status().json()
                archive_sha256 = sha256(archive).hexdigest()
                assert archive_sha256 == preview["archive_sha256"]
                response = client.post("/api/v1/packs/import", headers=headers | {
                    "Idempotency-Key": args.opening, "X-World-Pack-Sha256": preview["archive_sha256"],
                    "X-Story-Opening": args.opening, "X-Player-Setup": quote(json.dumps({"profile_id":"food-independent"})),
                }, content=archive)
                if response.is_error:
                    raise ValueError(f"World pack import returned {response.status_code}. {response.text}")
                world = response.json()
                version = preview["manifest"]["version"]
            branch_id = world["active_branch_id"]
            world_id = world["world_id"]
            path = f"/api/v1/worlds/{world_id}"
            actor_view = client.get(f"{path}/view").raise_for_status().json()
            if args.fork:
                branched = client.post(f"{path}/branches", headers={"Idempotency-Key": "audit-fork"}, json={
                    "branch_id": branch_id, "sequence": actor_view["last_seq"], "label": "旁观对照",
                }).raise_for_status().json()
                branch_id = branched["branch_id"]
                actor_view = client.get(f"{path}/view", params={"branch_id": branch_id}).raise_for_status().json()
            actor_id = actor_view["state"]["player_id"]
            start = actor_view["branch"]["current_tick"]
            record(world_id=world_id, branch_id=branch_id, actor_id=actor_id, start=start,
                   initial_hash=actor_view["state_hash"], genesis_sequence=actor_view["last_seq"],
                   pack=version, archive_sha256=archive_sha256, engine_sources_sha256=_engine_source_digest(), jobs=[])
        for i in range(chunk_count):
            if args.resume is not None:
                job_id = result["active_job"]["job_id"]
            else:
                actor_view = client.get(f"{path}/view", params={"branch_id": branch_id}).raise_for_status().json()
                version = actor_view["branch"]["state_version"]
                if args.mode == "quiet":
                    observation = client.post(f"{path}/messages", json={
                        "schema_version": "1.0", "request_id": str(uuid4()), "session_id": str(uuid4()),
                        "branch_id": branch_id, "actor_id": actor_id, "world_id": world_id, "expected_state_version": version,
                        "mode": "ask_gm", "text": "观察周围", "scene_observation": True,
                    }).raise_for_status().json()
                    assert observation["world_effect"] is False
                    assert observation["context_tick"] == actor_view["branch"]["current_tick"]
                if args.mode == "intervention" and i == 1:
                    intervention = _attempt_local_dialogue(client, path, actor_view, world_id, branch_id, actor_id)
                    record(intervention=intervention)
                    refreshed = client.get(f"{path}/view", params={"branch_id": branch_id}).raise_for_status().json()
                    assert refreshed["branch"]["current_tick"] == actor_view["branch"]["current_tick"]
                    version = refreshed["branch"]["state_version"]
                payload = {"branch_id": branch_id, "expected_state_version": version,
                           "profile": {"speed": "fast", "depth": "standard"},
                           "duration_seconds": args.duration // chunk_count + (i == chunk_count - 1) * (args.duration % chunk_count)}
                endpoint = "world-advance" if args.mode == "world" else "long-actions"
                if args.mode != "world":
                    payload["activity"] = "rest" if args.mode == "rest" else "wait"
                initial = client.post(f"{path}/{endpoint}", headers={"Idempotency-Key": f"{args.mode}-{i}"},
                                      json=payload).raise_for_status().json()
                job_id = initial["job_id"]
            deadline = monotonic() + args.timeout
            while True:
                job = client.get(f"/api/v1/jobs/{job_id}").raise_for_status().json()
                record(active_job=job, controlled_responses=decisions)
                if job["status"] not in {"queued", "running"} or monotonic() >= deadline:
                    break
                sleep(4)
            result["jobs"].append(job)
            if job["status"] in {"queued", "running"}:
                # The worker still owns the head. Keep the durable job for the next
                # run instead of comparing a moving snapshot to a moving replay.
                record(poll_timeout=True)
                raise SystemExit(124)
            if job["status"] != "succeeded":
                break
        state = repository.read_world(scope, UUID(world_id), UUID(branch_id))
        target = int(result["start"]) + args.duration
        target_reached = (state.state.current_tick == target
                          and int(result["jobs"][-1]["committed_through_tick"]) == target)
        record(last_tick=state.state.current_tick, final_hash=state.state_hash, last_seq=state.last_seq,
               actor_status=state.state.entities[UUID(actor_id)].status,
               calendar=state.state.calendar.model_dump(mode="json"), target_reached=target_reached,
               poll_timeout=False, replay_status="pending", replay_from_genesis=args.from_genesis)
        cursor = int(result["genesis_sequence"])
        event_counts = Counter()
        period_counts = {}
        period_people = {}
        period_terminal_tick = {}
        trace_periods = {}
        period_seconds = 30 * state.state.calendar.day_seconds
        command_counts = Counter()
        samples = {}
        review_samples = []
        private_npc_memories = 0
        reciprocal_replies = 0
        ambient_model_calls = set()
        ambient_topics = {}
        topic_followups = Counter()
        while True:
            page = repository.list_events(scope, UUID(world_id), UUID(branch_id), after_seq=cursor)
            for event in page.events:
                raw = event.model_dump(mode="json")
                event_type = raw["type"]
                event_counts[event_type] += 1
                tick = int(raw["tick"])
                period = min((tick - int(result["start"])) // period_seconds, (args.duration - 1) // period_seconds)
                counts = period_counts.setdefault(period, Counter())
                people = period_people.setdefault(period, set())
                period_terminal_tick[period] = tick
                counts[event_type] += 1
                payload = raw["payload"]
                if payload.get("model_call_id"):
                    trace_periods[payload["model_call_id"]] = period
                if event_type not in samples and "time" not in event_type:
                    samples[event_type] = raw
                if "organization_strategy_review" in event_type and len(review_samples) < 40:
                    review_samples.append(raw)
                if event_type == "core.ambient_event_resolved.v1":
                    outcome = payload["outcome"]
                    counts[f"ambient.{outcome}"] += 1
                    if outcome == "accepted":
                        proposal = payload["proposal"]
                        counts[f"ambient.kind.{proposal['kind']}"] += 1
                        ambient_model_calls.add(payload["model_call_id"])
                        participants = [proposal["initiator_id"], *proposal["participant_ids"]]
                        people.update(participants)
                        for participant in participants:
                            topic_id = uuid5(UUID(raw["event_id"]), f"learned:{participant}")
                            ambient_topics[str(topic_id)] = (period, proposal["kind"])
                if event_type == "core.social_contact_settled.v1":
                    assert project_event(raw, actor_id) is None
                    for choice in payload["choices"]:
                        counts[f"social.contact.{choice['outcome']}"] += 1
                        if choice["outcome"] != "kept_to_self":
                            people.update(choice["opportunity"]["participant_ids"])
                    counts["social.contact_score_changes"] += len(payload.get("relationship_effects", ()))
                if event_type == "core.knowledge_acquired.v1":
                    memory = payload["record"]
                    if memory["kind"] == "memory" and memory["visibility"]["policy"] == "subject_ids" and all(
                        memory.get(field) not in {None, actor_id} for field in ("owner_id", "holder_id")
                    ):
                        assert project_event(raw, actor_id) is None
                        private_npc_memories += 1
                        reciprocal_replies += bool(memory.get("speech_reply_to"))
                        people.update((memory["owner_id"], memory["holder_id"]))
                        for evidence_id in memory.get("source_refs", ()):
                            if evidence_id in ambient_topics:
                                origin_period, origin_kind = ambient_topics[evidence_id]
                                topic_followups[origin_kind] += 1
                                counts["ambient.topic_followup"] += 1
                                if origin_period != period:
                                    counts["ambient.topic_followup_after_origin_month"] += 1
                command_counts[str(raw["command_id"])] += 1
            if not page.has_more:
                break
            cursor = page.next_cursor
        cloud_summary = None
        if args.cloud:
            statuses = Counter()
            tokens = Counter()
            costs = {}
            successful_ids = set()
            unpriced_calls = 0
            duration_by_role = Counter()
            model_period_usage = {}
            cursor_key = None
            while True:
                calls = store.list_calls_page(scope, limit=500, cursor=cursor_key)
                for trace in calls["items"]:
                    assert trace["provider_id"] == "deepseek" and trace["provider_kind"] == "deepseek"
                    if trace["world_id"] != world_id or trace["branch_id"] != branch_id:
                        continue
                    error = trace.get("error_code")
                    statuses[f"{trace['role_task']}.{error or 'success'}"] += 1
                    remote_duration = (datetime.fromisoformat(trace["ended_at"]) - datetime.fromisoformat(trace["started_at"])).total_seconds()
                    duration_by_role[trace["role_task"]] += max(0.0, remote_duration)
                    model_period = trace_periods.get(trace["request_id"])
                    period_usage = model_period_usage.setdefault("unattributed" if model_period is None else str(model_period), Counter())
                    period_usage[f"{trace['role_task']}.{error or 'success'}"] += 1
                    period_usage["remote_seconds"] += max(0.0, remote_duration)
                    if trace.get("queue_seconds") is not None:
                        period_usage["queue_seconds"] += trace["queue_seconds"]
                    if not error:
                        successful_ids.add(trace["request_id"])
                    for key, value in (trace.get("usage") or {}).items():
                        if type(value) is int:
                            tokens[key] += value
                            period_usage[key] += value
                    cost = trace.get("cost") or {}
                    if cost.get("amount") is not None and cost.get("currency"):
                        currency = cost["currency"]
                        costs[currency] = costs.get(currency, Decimal(0)) + Decimal(str(cost["amount"]))
                    else:
                        unpriced_calls += 1
                if not calls["has_more"]:
                    break
                cursor_key = calls["next_cursor"]
            assert ambient_model_calls <= successful_ids
            cloud_summary = {"call_status_by_role": statuses, "usage": tokens, "unpriced_calls": unpriced_calls,
                             "estimated_cost": {key: str(amount) for key, amount in costs.items()},
                             "successful_calls": len(successful_ids), "accepted_ambient_calls": len(ambient_model_calls),
                             "remote_seconds_by_role": duration_by_role, "attributed_activity_by_period": model_period_usage}
        participating_npcs = set().union(*period_people.values())
        record(events=event_counts, controlled_responses=decisions, real_cloud=cloud_summary,
               period_duration_seconds=period_seconds, events_by_period=period_counts,
               terminal_tick_by_period=period_terminal_tick,
               npc_participants_by_period={period: len(people) for period, people in period_people.items()},
               ambient_topic_followups=topic_followups,
               private_npc_memories=private_npc_memories, reciprocal_replies=reciprocal_replies,
               autonomous_participants=len(participating_npcs),
               samples=samples, strategy_reviews=review_samples, distinct_commands=len(command_counts))
        if timings is not None:
            record(storage_timings=timings())
        replay_start = perf_counter()
        replay = repository.replay(scope, UUID(world_id), UUID(branch_id), from_genesis=args.from_genesis)
        record(replay_equal=replay.state_hash == state.state_hash, replay_status="checked", replay_seconds=perf_counter() - replay_start)

        print(output, state.state.current_tick, event_counts, decisions, flush=True)
        if (not target_reached or state.state_hash != replay.state_hash
                or any(job["status"] != "succeeded" for job in result["jobs"])
                or (args.mode == "intervention" and not result.get("intervention", {}).get("committed_dialogue"))
                or (args.cloud and not cloud_summary["successful_calls"])):
            raise SystemExit(1)


def _attempt_local_dialogue(client, path, view, world_id, branch_id, actor_id):
    entities = view["state"]["entities"]
    actor = entities[actor_id]
    local_npcs = sorted(
        (person for person in entities.values()
         if person["id"] != actor_id and person["kind"] in {"character", "person"}
         and person.get("location_id") == actor.get("location_id") and person.get("status") == "active"
         and person.get("name")),
        key=lambda person: (person["name"], person["id"]),
    )
    for target in local_npcs[:3]:
        result = client.post(f"{path}/messages", json={
            "schema_version": "1.0", "request_id": str(uuid4()), "session_id": str(uuid4()),
            "world_id": world_id, "branch_id": branch_id, "actor_id": actor_id,
            "expected_state_version": view["branch"]["state_version"], "profile": {"speed": "fast", "depth": "standard"},
            "mode": "roleplay", "text": f"我向{target['name']}打招呼，问这里最近有没有什么值得留意的事情。",
        }).raise_for_status().json()
        if result["kind"] == "dialogue" and result["world_effect"]:
            assert result["references"] == [target["id"]] and result["elapsed_ticks"] == "0"
            return {"committed_dialogue": True, "target": target["name"], "tick": view["branch"]["current_tick"]}
    return {"committed_dialogue": False, "visible_local_candidates": len(local_npcs)}


def _engine_source_digest():
    root = Path(__file__).resolve().parents[1]
    paths = [*sorted((root / "backend" / "world_engine").rglob("*.py")), root / "requirements.lock.txt", Path(__file__)]
    digest = sha256()
    for path in paths:
        digest.update(path.relative_to(root).as_posix().encode("utf-8") + b"\x00")
        data = path.read_bytes().replace(b"\r\n", b"\n")
        digest.update(sha256(data).digest())
    return digest.hexdigest()


def _measure_storage_phases():
    from world_engine.domain import reducers
    from world_engine.simulation import engine
    from world_engine.storage import repository

    lock = Lock()
    counts = Counter()
    durations = Counter()

    def summarize():
        with lock:
            return {key: {"calls": counts[key], "inclusive_seconds": durations[key]} for key in sorted(counts)}

    @contextmanager
    def measure(label):
        started_at = perf_counter()
        try:
            yield
        finally:
            elapsed = perf_counter() - started_at
            with lock:
                counts[label] += 1
                durations[label] += elapsed

    def wrap(target, name, label):
        original = getattr(target, name)

        @wraps(original)
        def timed(*args, **kwargs):
            with measure(label):
                return original(*args, **kwargs)

        setattr(target, name, timed)

    transaction = repository.WorldRepository._transaction

    @contextmanager
    def timed_transaction(self, *args, **kwargs):
        with measure("sqlite_write_transaction" if kwargs.get("write") else "sqlite_read_transaction"):
            with transaction(self, *args, **kwargs) as session:
                yield session

    repository.WorldRepository._transaction = timed_transaction
    wrap(reducers, "_canonical_state_payload", "canonical_state_serialization")
    wrap(reducers, "_validated_state", "state_validation_in_reducer")
    wrap(engine.SimulationEngine, "plan_window", "simulation_plan_window")
    wrap(repository.WorldRepository, "commit_command", "command_commit")
    return summarize


if __name__ == "__main__":
    main()
