"""Exercise real time advancement and replay with data-driven, offline model responses."""

import argparse
from collections import Counter
import json
import os
from pathlib import Path
import re
from tempfile import mkdtemp
from time import monotonic, sleep
from types import SimpleNamespace
from uuid import UUID, uuid4
from urllib.parse import quote

from fastapi.testclient import TestClient
from world_engine.adapters.models.contracts import RoleTask
from world_engine.adapters.models.errors import ProviderError, ProviderErrorCode
from world_engine.application.operations import ModelOperations
from world_engine.application.visibility import project_event
from world_engine.runtime import build_application, local_identity


BUILTIN_BACKGROUNDS = frozenset({"modern_city", "low_magic_feudal", "long_life_cultivation"})


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("opening")
    parser.add_argument("mode", choices=("world", "rest", "quiet"))
    parser.add_argument("duration", type=int)
    parser.add_argument("--timeout", type=int, default=3600)
    parser.add_argument("--archive", default=".runtime/world-packs/gensokyo-world-1.5.0.zip")
    parser.add_argument("--seed", type=int, default=19, help="Seed for a built-in background.")
    parser.add_argument("--fork", action="store_true", help="Create a branch at the unmodified opening before advancing.")
    parser.add_argument("--resume", type=Path, help="Resume one unfinished rest or world job from its isolated evidence directory.")
    parser.add_argument("--from-genesis", action="store_true", help="Verify the entire terminal ledger, including all earlier checkpoints.")
    parser.add_argument("--provider-unavailable", action="store_true", help="Exercise the native deferral policy without any model responses.")
    args = parser.parse_args()
    if not re.fullmatch(r"[a-z][a-z0-9_-]{0,79}", args.opening):
        parser.error("Use a registered built-in background or story opening identifier.")
    if args.timeout < 1 or args.duration < (3 if args.mode == "quiet" else 1):
        parser.error("Choose a positive timeout and duration for every requested segment.")
    if os.environ.get("WORLD_ENGINE_DATABASE_URL"):
        parser.error("An external database override cannot be used for an isolated verification run.")
    evidence = Path(".runtime/autonomy-evidence")
    evidence.mkdir(parents=True, exist_ok=True)
    if args.resume is None:
        data = Path(mkdtemp(prefix=f"controlled-{args.opening}-{args.mode}-", dir=evidence))
        result = {"scope": "isolated official API, built-in worker, controlled data-driven planning responses; no model service",
                  "opening": args.opening, "mode": args.mode, "duration": args.duration, "database": str(data),
                  "provider_unavailable": args.provider_unavailable, "response_observations_complete": True}
    else:
        data = args.resume.resolve()
        if args.mode == "quiet" or data.parent != evidence.resolve() or not data.name.startswith("controlled-"):
            parser.error("Resume accepts one isolated rest or world run inside .runtime/autonomy-evidence.")
        result = json.loads((data / "evidence.json").read_text(encoding="utf-8"))
        if (result.get("opening"), result.get("mode"), result.get("duration")) != (args.opening, args.mode, args.duration):
            parser.error("Use the opening, mode, and duration from the original run.")
        if "active_job" not in result:
            parser.error("The previous run did not submit a durable job.")
        if result.get("provider_unavailable", False) != args.provider_unavailable:
            parser.error("Use the same provider mode as the original run.")
        result.setdefault("response_observations_complete", False)
    if args.provider_unavailable:
        result["scope"] = "isolated official API and built-in worker with injected model unavailability; no model service"
    output = data / "evidence.json"
    origin = "http://127.0.0.1:39872"
    app = build_application(data_directory=data, port=39872, compact_ledger=True)
    scope = local_identity(data)
    decisions = Counter(result.get("controlled_responses", {}))

    async def generate(_self, _scope, request):
        data = json.loads(request.messages[-1].content)
        role = request.role_task
        decisions[str(role.value)] += 1
        if args.provider_unavailable:
            raise ProviderError(ProviderErrorCode.UNAVAILABLE, "Provider unavailable in isolated simulation verification.")
        if role is RoleTask.NPC_PLAN_REVIEW:
            return SimpleNamespace(normalized_output={"verdict": "approve"})
        if role is RoleTask.NPC_DECISION:
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
            if args.opening in BUILTIN_BACKGROUNDS:
                world = client.post("/api/v1/worlds", headers={"Idempotency-Key": "builtin-strategy"}, json={
                    "title": "机构自治对照", "player_name": "旅人", "pack_id": args.opening,
                    "pack_version": "2.13.0", "seed": args.seed,
                }).raise_for_status().json()
                version = "2.13.0"
            else:
                archive = Path(args.archive).read_bytes()
                headers = {"Content-Type": "application/zip", "Origin": origin}
                preview = client.post("/api/v1/packs/import/preview", headers=headers, content=archive).raise_for_status().json()
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
                   pack=version, jobs=[])
        chunks = 3 if args.mode == "quiet" else 1
        for i in range(chunks):
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
                payload = {"branch_id": branch_id, "expected_state_version": version,
                           "profile": {"speed": "fast", "depth": "standard"},
                           "duration_seconds": args.duration // chunks + (i == chunks - 1) * (args.duration % chunks)}
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
        period_seconds = 30 * state.state.calendar.day_seconds
        command_counts = Counter()
        samples = {}
        review_samples = []
        private_npc_memories = 0
        reciprocal_replies = 0
        while True:
            page = repository.list_events(scope, UUID(world_id), UUID(branch_id), after_seq=cursor)
            for event in page.events:
                raw = event.model_dump(mode="json")
                event_type = raw["type"]
                event_counts[event_type] += 1
                period = (int(raw["tick"]) - int(result["start"])) // period_seconds
                period_counts.setdefault(period, Counter())[event_type] += 1
                if event_type not in samples and "time" not in event_type:
                    samples[event_type] = raw
                if "organization_strategy_review" in event_type and len(review_samples) < 40:
                    review_samples.append(raw)
                if event_type == "core.knowledge_acquired.v1":
                    memory = raw["payload"]["record"]
                    if memory["kind"] == "memory" and memory["visibility"]["policy"] == "subject_ids" and all(
                        memory.get(field) not in {None, actor_id} for field in ("owner_id", "holder_id")
                    ):
                        assert project_event(raw, actor_id) is None
                        private_npc_memories += 1
                        reciprocal_replies += bool(memory.get("speech_reply_to"))
                command_counts[str(raw["command_id"])] += 1
            if not page.has_more:
                break
            cursor = page.next_cursor
        record(events=event_counts, controlled_responses=decisions,
               period_duration_seconds=period_seconds, events_by_period=period_counts,
               private_npc_memories=private_npc_memories, reciprocal_replies=reciprocal_replies,
               samples=samples, strategy_reviews=review_samples, distinct_commands=len(command_counts))
        replay = repository.replay(scope, UUID(world_id), UUID(branch_id), from_genesis=args.from_genesis)
        record(replay_equal=replay.state_hash == state.state_hash, replay_status="checked")

        print(output, state.state.current_tick, event_counts, decisions, flush=True)
        if (not target_reached or state.state_hash != replay.state_hash
                or any(job["status"] != "succeeded" for job in result["jobs"])):
            raise SystemExit(1)


if __name__ == "__main__":
    main()
