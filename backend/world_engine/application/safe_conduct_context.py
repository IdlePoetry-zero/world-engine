"""Public treaty proposals scoped to the actor and visible route origins."""

import re
import unicodedata
from uuid import UUID, uuid5

from world_engine.domain.components import PlaceRoute
from world_engine.domain.models import WorldState
from world_engine.domain.safe_conduct import SAFE_CONDUCT_TAG, SafeConductError, propose_safe_conduct, transition_safe_conduct


_TREATY_ID = re.compile(r"(?<![0-9a-f])[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}(?![0-9a-f])")


def safe_conduct_closure_matches(text: str, treaty_id: UUID, operation: str, treaties: list[dict]) -> bool:
    """Require a source-grounded selection before closing a treaty from chat."""
    source = unicodedata.normalize("NFKC", text).casefold()
    ids = set(_TREATY_ID.findall(source))
    if ids:
        return ids == {str(treaty_id)}

    eligible_statuses = {"pending", "countered"} if operation == "withdraw" else {"active"}
    candidates = [row for row in treaties if row["status"] in eligible_statuses]
    selections: list[set[str]] = []
    for field in ("recipient_name", "to_place_name", "from_place_name", "proposer_name"):
        matching_ids = {
            row["treaty_id"] for row in candidates
            if row.get(field) and unicodedata.normalize("NFKC", row[field]).casefold() in source
        }
        if matching_ids and (field in {"recipient_name", "to_place_name"} or selections):
            selections.append(matching_ids)
    return bool(selections) and set.intersection(*selections) == {str(treaty_id)}


def safe_conduct_response_feedback(operation: str, counter_expiry: int | None) -> str:
    if operation == "counter":
        return f"对方提出把到期时间缩短至世界第 {counter_expiry} 秒，等待你接受。"
    if operation == "ratify":
        return "对方批准了协定，通行许可已生效。"
    return "对方拒绝了协定，路线权限保持原状。"


def safe_conduct_action_context(state: WorldState, visible: dict, actor_id: str, *, can_host: bool = True) -> dict:
    records = {str(row.treaty_id): row for row in state.safe_conduct_treaties}
    treaties = []
    actor = UUID(actor_id)
    for row in visible["own_safe_conduct_treaties"]:
        treaty = records[row["treaty_id"]]
        can_ratify = False
        if treaty.status == "pending" and not row["expired"] and (
            str(treaty.recipient_actor_id) == actor_id or row["can_request_response"]
        ):
            try:
                transition_safe_conduct(
                    state, treaty, actor_id=treaty.recipient_actor_id, operation="ratify", expected_revision=treaty.revision,
                )
            except SafeConductError:
                pass
            else:
                can_ratify = True
        can_accept_counter = False
        if treaty.status == "countered" and not row["expired"] and str(treaty.proposer_actor_id) == actor_id:
            try:
                transition_safe_conduct(
                    state, treaty, actor_id=treaty.proposer_actor_id, operation="accept_counter", expected_revision=treaty.revision,
                )
            except SafeConductError:
                pass
            else:
                can_accept_counter = True
        can_withdraw = False
        if treaty.status in {"pending", "countered"}:
            try:
                transition_safe_conduct(state, treaty, actor_id=actor, operation="withdraw", expected_revision=treaty.revision)
            except SafeConductError:
                pass
            else:
                can_withdraw = True
        treaties.append(row | {
            "can_ratify": can_ratify and str(treaty.recipient_actor_id) == actor_id,
            "can_counter": can_ratify and str(treaty.recipient_actor_id) == actor_id and state.current_tick + 1 < treaty.expires_at_tick,
            "can_accept_counter": can_accept_counter,
            "can_request_response": can_host and can_ratify and row["can_request_response"],
            "can_withdraw": can_withdraw,
        })
    return {
        "own_safe_conduct_treaties": treaties,
        "safe_conduct_proposal_options": safe_conduct_proposal_options(state, visible, actor_id),
    }


def safe_conduct_proposal_options(state: WorldState, visible: dict, actor_id: str) -> list[dict[str, str]]:
    if state.current_tick >= 2**63 - 1:
        return []
    sources = [row for row in visible["own_memberships"] if row["is_leader"]]
    if not sources:
        return []
    actor = UUID(actor_id)
    public_organizations = [
        item for item in state.entities.values()
        if item.kind in {"organization", "polity", "faction", "company", "government"}
        and item.status == "active" and not item.components.get("hidden")
    ]
    options: list[dict[str, str]] = []
    for source in sources:
        source_id = UUID(source["organization_id"])
        for entity_id in visible["entities"]:
            origin = state.entities[UUID(entity_id)]
            if origin.kind not in {"place", "location"}:
                continue
            for raw_route in origin.components.get("place", {}).get("routes", []):
                if SAFE_CONDUCT_TAG not in raw_route.get("access_tags", []):
                    continue
                route = PlaceRoute.model_validate(raw_route)
                destination = state.entities.get(route.to_place_id)
                if destination is None:
                    continue
                place = destination.components.get("place", {})
                region_id = str(place.get("region_id") or destination.id)
                for recipient in public_organizations:
                    if recipient.id == source_id or str(recipient.location_id) != region_id:
                        continue
                    try:
                        propose_safe_conduct(
                            state, treaty_id=uuid5(actor, f"safe-conduct-option:{route.route_id}:{recipient.id}"),
                            proposer_organization_id=source_id, recipient_organization_id=recipient.id,
                            proposer_actor_id=actor, route=route, expires_at_tick=state.current_tick + 1,
                            existing_treaties=state.safe_conduct_treaties,
                        )
                    except SafeConductError:
                        continue
                    options.append({
                        "proposer_organization_id": str(source_id), "proposer_name": source["name"],
                        "recipient_organization_id": str(recipient.id), "recipient_name": recipient.name,
                        "from_place_id": str(origin.id), "from_place_name": origin.name,
                        "to_place_id": str(destination.id), "to_place_name": destination.name,
                        "route_id": str(route.route_id),
                    })
    return options
