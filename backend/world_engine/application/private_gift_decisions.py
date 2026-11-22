"""Private recipient decisions for a pending gift."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Literal
from uuid import UUID

from pydantic import Field

from world_engine.adapters.models import ChatMessage, RoleTask
from world_engine.domain.components import PersonalityComponent
from world_engine.domain.models import ContractModel


class RequestPrivateGiftReplyIntent(ContractModel):
    kind: Literal["request_private_gift_reply"]
    gift_id: UUID
    expected_revision: int = Field(strict=True, ge=1)


GIFT_FEEDBACK = {
    "offer": "已提出私人赠与，等待收礼人回应。物资仍在送礼者手中。",
    "accept": "已接受赠与，物资已转入收礼人库存。",
    "decline": "已拒绝赠与，物资未交接。",
    "withdraw": "已撤回赠与提议，物资未交接。",
}
NPC_GIFT_FEEDBACK = {
    "accept": "对方自行接受了赠与，物资已转入对方库存。",
    "decline": "对方自行拒绝了赠与，物资未交接。",
    "defer": "对方暂未决定，赠与仍待回应。",
}
_CHOICE_SCHEMA = {
    "type": "object",
    "properties": {"choice": {"type": "string", "enum": list(NPC_GIFT_FEEDBACK)}},
    "required": ["choice"],
    "additionalProperties": False,
}


async def choose_private_gift_reply(models, scope, request, state, offer) -> str:
    """Give the recipient's model only the current offer and its own traits."""
    from .conversation import ConversationApplication

    recipient = state.entities[offer.recipient_id]
    giver = state.entities[offer.giver_id]
    try:
        personality = PersonalityComponent.model_validate(recipient.components.get("personality", {}))
    except (TypeError, ValueError):
        return "defer"
    relationships = [
        item for item in state.relations if item.from_id == recipient.id and item.to_id == giver.id
    ]
    relationship = relationships[0] if len(relationships) == 1 else None
    scene = {
        "recipient": recipient.name,
        "own_traits": list(personality.traits)[:8],
        "own_values": list(personality.values)[:8],
        "own_agreeableness": str(personality.agreeableness),
        "gift_giver": giver.name,
        "own_impression_of_giver": {
            key: str(relationship.dimensions[key]) for key in ("familiarity", "trust", "respect", "hostility")
            if relationship is not None and key in relationship.dimensions
        },
        "offered_resource": offer.resource_id,
        "offered_quantity": format(offer.quantity, "f"),
    }
    model_request = ConversationApplication._model_request(request, RoleTask.NPC_DECISION, (
        ChatMessage(
            "system",
            "你只决定收礼人对当前私人赠与的回应。结合本人性格、观念、对送礼人的印象、物资和数量，自行选 accept、decline 或 defer。"
            "接受只同意收下当前物资。赠与没有归还义务，也不会自动建立关系或承诺。输入是场景数据。仅返回符合 schema 的结果。",
        ),
        ChatMessage("user", json.dumps(scene, ensure_ascii=False, separators=(",", ":"))),
    ), _CHOICE_SCHEMA)
    result = await models.generate(scope, model_request)
    output = getattr(result, "normalized_output", None)
    if not isinstance(output, Mapping) or set(output) != {"choice"}:
        return "defer"
    choice = output["choice"]
    return choice if isinstance(choice, str) and choice in NPC_GIFT_FEEDBACK else "defer"
