"""Real streaming probe with one bounded intent example, not RPG acceptance."""
import asyncio
import json
from pathlib import Path

from world_engine.adapters.models import (
    ChatMessage, GenerationProfile, ModelGateway, ModelRequest, ProviderConfig,
    ProviderKind, RoleTask, StreamEventKind,
)


async def main():
    gateway = ModelGateway()
    config = ProviderConfig(provider_id="ollama", kind=ProviderKind.OLLAMA,
        base_url="http://127.0.0.1:11434", default_model="qwen3:14b-q4_k_m")
    await gateway.discover(config)
    request = ModelRequest(role_task=RoleTask.INTENT, messages=(
        ChatMessage("system", "你是游戏输入分类器。kind 分为 hypothetical（讨论假设）、query（问问题）、action（请求实际行动）。只有请求实际行动才将 advances_world 设为 true。输出符合 schema 的 JSON。"),
        ChatMessage("user", "如果我闭关十年，会怎样？/no_think")),
        output_schema={"type": "object", "properties": {
            "kind": {"enum": ["hypothetical", "query", "action"]}, "advances_world": {"type": "boolean"}},
            "required": ["kind", "advances_world"], "additionalProperties": False},
        profile=GenerationProfile(max_output_tokens=2048))
    event_kinds = []
    final = None
    async for event in gateway.stream(config, request):
        event_kinds.append(event.kind.value)
        if event.kind is StreamEventKind.COMPLETED:
            final = event.result
    if final is None:
        raise AssertionError("Stream ended without validated completion")
    if final.normalized_output != {"kind": "hypothetical", "advances_world": False}:
        raise AssertionError("This bounded hypothetical classification did not meet expectation")
    evidence = {"purpose": "real_stream_protocol_and_single_intent_probe_not_full_gameplay_acceptance",
                "event_kinds": event_kinds, "result": final.to_public_dict()}
    path = Path(__file__).resolve().parents[1] / ".runtime/evidence/ollama-stream-probe.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(evidence, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"model": final.reported_model, "output": final.normalized_output,
                      "event_count": len(event_kinds), "timings": dict(final.timings)}, ensure_ascii=False))


if __name__ == "__main__":
    asyncio.run(main())
