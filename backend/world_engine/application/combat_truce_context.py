"""Participant-scoped action context for resolving a fictional encounter by agreement."""

from uuid import UUID

from world_engine.domain.combat import active_combat_opponent
from world_engine.domain.combat_truce import current_combat_truce_offer
from world_engine.domain.models import WorldState


TRUCE_FEEDBACK = {
    "offer": "已向对方提出停战，等待回应。",
    "accept": "双方已同意停止这次交战。",
    "decline": "停战提议已被拒绝，当前遭遇仍在继续。",
    "defer": "对方还没有明确回应，停战提议仍然有效。",
}


def combat_truce_options(state: WorldState, actor_id: UUID, visible_entities: dict) -> dict | None:
    actor = state.entities.get(actor_id)
    if actor is None or actor.status != "active":
        return None
    pair = active_combat_opponent(state, actor)
    if pair is None or str(pair[1].id) not in visible_entities:
        return None
    encounter, opponent = pair
    pending = current_combat_truce_offer(state, actor_id, state.combat_truce_offers)
    return {
        "opponent_id": str(opponent.id),
        "opponent_name": opponent.name,
        "expected_last_event_id": str(encounter.last_event_id),
        "offer_id": str(pending.offer_id) if pending is not None else None,
        "proposed_by_self": pending.proposer_id == actor_id if pending is not None else False,
    }
