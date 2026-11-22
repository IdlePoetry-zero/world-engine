"""Real DeepSeek probe using an existing OS credential reference only."""
import asyncio
import json
from pathlib import Path

from world_engine.adapters.credentials import WindowsCredentialStore
from world_engine.adapters.models import ChatMessage, GenerationProfile, ModelGateway, ModelRequest, RoleTask
from world_engine.adapters.models.errors import ProviderError
from world_engine.application.credentials import AccountCredentialResolver, CredentialService
from world_engine.application.operations import ModelOperations, OperationsStore
from world_engine.application.pricing import bundled_price
from world_engine.runtime import local_identity


async def main():
    root = Path(__file__).resolve().parents[1]
    identity = local_identity(root / ".data")
    store = OperationsStore(root / ".data/operations.db")
    credentials = CredentialService(store, WindowsCredentialStore())
    gateway = ModelGateway(secret_resolver=AccountCredentialResolver(credentials, identity), price_resolver=bundled_price)
    operations = ModelOperations(store, gateway)
    request = ModelRequest(role_task=RoleTask.INTENT,
        messages=(ChatMessage("system", "你是 RPG 输入分类器。假设讨论用 hypothetical，明确要求实际执行用 action，普通问题用 query。只有 action 的 advances_world 为 true。只输出 JSON。"),
                  ChatMessage("user", "如果我闭关十年，会怎样？")),
        output_schema={"type": "object", "properties": {"kind": {"enum": ["hypothetical", "action", "query"]},
            "advances_world": {"type": "boolean"}}, "required": ["kind", "advances_world"], "additionalProperties": False},
        profile=GenerationProfile(max_output_tokens=4096))
    try:
        result = await operations.generate(identity, request)
    except ProviderError as error:
        print(json.dumps({"status": "failed", "code": error.code.value, "http_status": error.status_code}))
        raise SystemExit(1) from None
    if result.normalized_output != {"kind": "hypothetical", "advances_world": False}:
        raise AssertionError("The single hypothetical classification did not meet expectation")
    evidence = {"purpose": "real_deepseek_protocol_and_single_intent_probe_not_full_gameplay_acceptance",
                "result": result.to_public_dict()}
    path = root / ".runtime/evidence/deepseek-pricing-probe.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(evidence, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"status": "succeeded", "requested_model": result.requested_model, "reported_model": result.reported_model,
        "output": result.normalized_output, "usage": result.to_public_dict()["provider_usage"],
        "timings": dict(result.timings), "cost": result.cost.to_public_dict()}, ensure_ascii=False))


if __name__ == "__main__":
    asyncio.run(main())
