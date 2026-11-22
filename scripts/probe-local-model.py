"""Real local gateway smoke test; writes protocol evidence, not world history."""
import asyncio
import json
from pathlib import Path

from world_engine.adapters.models import (
    ChatMessage, GenerationProfile, ModelGateway, ModelRequest,
    ProviderConfig, ProviderKind, RoleTask,
)


async def main():
    gateway = ModelGateway()
    config = ProviderConfig(provider_id="ollama", kind=ProviderKind.OLLAMA,
                            base_url="http://127.0.0.1:11434", default_model="qwen3:8b-q4_k_m")
    models = await gateway.discover(config)
    request = ModelRequest(role_task=RoleTask.INTENT, messages=(ChatMessage("user",
        '请判断“如果我闭关十年会怎样？”是询问假设还是实际行动。只输出 JSON：{"kind":"hypothetical","advances_world":false}。/no_think'),),
        output_schema={"type": "object", "properties": {"kind": {"const": "hypothetical"},
        "advances_world": {"const": False}}, "required": ["kind", "advances_world"], "additionalProperties": False},
        profile=GenerationProfile(max_output_tokens=2048))
    result = await gateway.generate(config, request)
    evidence = {"purpose": "real_local_protocol_smoke_test_not_gameplay_acceptance",
                "models": [model.model_id for model in models], "result": result.to_public_dict()}
    destination = Path(__file__).resolve().parents[1] / ".runtime" / "evidence" / "ollama-probe.json"
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(evidence, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"model": result.reported_model, "output": result.normalized_output,
                      "usage": result.provider_usage.to_public_dict() if hasattr(result.provider_usage, "to_public_dict") else result.to_public_dict()["provider_usage"],
                      "timings": dict(result.timings)}, ensure_ascii=False))


if __name__ == "__main__":
    asyncio.run(main())
