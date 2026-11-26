"""Emit publishable aggregates from the isolated world verification receipts."""

import argparse
from datetime import datetime
import json
import math
from pathlib import Path
import re


_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_LABEL = re.compile(r"[a-zA-Z0-9_.-]{1,150}\Z")
_PRICE = re.compile(r"[0-9]{1,20}(\.[0-9]{1,15})?\Z")


def _safe_numbers(values):
    return {
        key: value for key, value in values.items()
        if _LABEL.fullmatch(key) and type(value) in {int, float} and math.isfinite(value) and value >= 0
    }


def summarize(receipt):
    calendar = receipt["calendar"]
    mode, opening, pack = (receipt[key] for key in ("mode", "opening", "pack"))
    if any(not _LABEL.fullmatch(value) for value in (mode, opening, pack)):
        raise ValueError("The receipt has an invalid opening or package label")
    if receipt.get("replay_status") != "checked" or "last_tick" not in receipt:
        raise ValueError("Finish the run and its replay before preparing evidence")
    start = int(receipt["start"])
    end = int(receipt["last_tick"])
    jobs = receipt["jobs"]
    cloud = receipt.get("real_cloud")
    result = {
        "opening": opening, "mode": mode, "world_pack_version": pack,
        "calendar": {"day_seconds": int(calendar["day_seconds"]), "year_days": int(calendar["year_days"])},
        "duration_requested_seconds": int(receipt["duration"]), "start_tick": start, "end_tick": end,
        "elapsed_world_days": (end - start) / calendar["day_seconds"],
        "target_reached": receipt.get("target_reached") is True,
        "replay_equal": receipt.get("replay_equal") is True,
        "replay_from_genesis": receipt.get("replay_from_genesis") is True,
        "replay_seconds": receipt.get("replay_seconds"),
        "wall_seconds": (datetime.fromisoformat(jobs[-1]["updated_at"]) - datetime.fromisoformat(jobs[0]["created_at"])).total_seconds(),
        "job_states": [row["status"] for row in jobs if _LABEL.fullmatch(row["status"])],
        "last_sequence": int(receipt["last_seq"]), "committed_commands": int(receipt["distinct_commands"]),
        "private_npc_memory_records": int(receipt["private_npc_memories"]),
        "reciprocal_reply_records": int(receipt["reciprocal_replies"]),
        "autonomous_npc_participants": int(receipt["autonomous_participants"]),
        "player_dialogue_committed": receipt.get("intervention", {}).get("committed_dialogue") is True,
        "events": _safe_numbers(receipt["events"]),
        "events_by_period": {key: _safe_numbers(value) for key, value in receipt["events_by_period"].items() if key.isdecimal()},
        "terminal_tick_by_period": _safe_numbers(receipt.get("terminal_tick_by_period", {})),
        "npc_participants_by_period": _safe_numbers(receipt.get("npc_participants_by_period", {})),
        "ambient_topic_followups": _safe_numbers(receipt.get("ambient_topic_followups", {})),
        "storage_timings": {key: _safe_numbers(value) for key, value in receipt.get("storage_timings", {}).items() if _LABEL.fullmatch(key)},
    }
    for key in ("initial_hash", "final_hash", "archive_sha256", "engine_sources_sha256"):
        digest = receipt.get(key)
        if isinstance(digest, str) and _DIGEST.fullmatch(digest):
            result[key] = digest
    if receipt.get("cloud"):
        if cloud is None:
            raise ValueError("The cloud receipt has no completed call evidence")
        result["model_evidence"] = {
            "provider": "DeepSeek", "status_by_role": _safe_numbers(cloud["call_status_by_role"]),
            "used_cloud": bool(cloud["successful_calls"]), "used_local_model": False,
            "successful_calls": int(cloud["successful_calls"]), "accepted_ambient_calls": int(cloud["accepted_ambient_calls"]),
            "usage": _safe_numbers(cloud["usage"]), "unpriced_calls": int(cloud["unpriced_calls"]),
            "estimated_cost": {key: value for key, value in cloud["estimated_cost"].items()
                               if re.fullmatch(r"[A-Z]{3}", key) and _PRICE.fullmatch(value)},
            "remote_seconds_by_role": _safe_numbers(cloud.get("remote_seconds_by_role", {})),
            "attributed_activity_by_period": {
                key: _safe_numbers(values) for key, values in cloud.get("attributed_activity_by_period", {}).items()
                if key.isdecimal() or key == "unattributed"
            },
        }
    else:
        result["model_evidence"] = {"used_cloud": False, "used_local_model": False, "controlled_fixture": True}
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("receipts", nargs="+", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    summaries = [summarize(json.loads(path.read_text(encoding="utf-8"))) for path in args.receipts]
    output = json.dumps(summaries, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
    if args.output is not None:
        args.output.write_text(output, encoding="utf-8")
    else:
        print(output, end="")


if __name__ == "__main__":
    main()
