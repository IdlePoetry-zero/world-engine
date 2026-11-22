from __future__ import annotations

from uuid import UUID, uuid5

import pytest

from world_engine.content.investigation_openings import build_opening_case
from world_engine.domain.investigation_cases import InvestigationCaseDefinition
from world_engine.domain.models import WorldState


_NAMESPACE = UUID("a18899d0-68da-4e9f-8f82-d693b7453c0e")
_PACKS = ("low_magic_feudal", "modern_city", "long_life_cultivation")


def _entity_id(*parts: object) -> UUID:
    return uuid5(_NAMESPACE, ":".join(str(part) for part in parts))


@pytest.mark.parametrize("pack_id", _PACKS)
def test_opening_case_draft_is_typed_and_valid_in_world(pack_id: str) -> None:
    opening_id = uuid5(_NAMESPACE, "place:opening")
    discovery_site_id = uuid5(_NAMESPACE, "place:discovery")
    player_id = uuid5(_NAMESPACE, "character:player")
    witness_id = uuid5(_NAMESPACE, "character:witness")
    alternate_witness_id = uuid5(_NAMESPACE, "character:alternate")

    draft = build_opening_case(
        pack_id,
        "2.0.0",
        opening_id,
        discovery_site_id,
        witness_id,
        alternate_witness_id,
        _entity_id,
    )

    definition = InvestigationCaseDefinition.model_validate(draft.anchor_components["investigation_case"])
    assert len(definition.routes) == 3
    assert definition.routes[0].suspect_id == witness_id
    assert definition.routes[0].requirements[0].effective_tick == -3_600
    assert definition.routes[0].requirements[0].observation_provenance[0].reference_id == f"{pack_id}:2.0.0:opening.archive"
    assert definition.routes[0].requirements[0].claim_provenance[0].reference_id == f"{pack_id}:2.0.0:opening.witness"
    assert definition.routes[1].suspect_id == witness_id
    assert definition.routes[1].requirements[0].observation_provenance[0].reference_id == f"{pack_id}:2.0.0:opening.register"
    assert definition.routes[1].requirements[0].claim_provenance[0].reference_id == f"{pack_id}:2.0.0:opening.echo"
    alibi_route = definition.routes[2]
    assert alibi_route.finding == "rules_out"
    assert alibi_route.suspect_id == alternate_witness_id
    assert alibi_route.requirements[0].predicate == "entity_location"
    assert alibi_route.requirements[0].finding == "corroborated"
    assert definition.wrong_suspect_effect is not None
    assert definition.wrong_suspect_effect.dimension == "trust"
    assert definition.wrong_suspect_effect.delta < 0

    base_state = WorldState.model_validate({
        "player_id": str(player_id),
        "entities": {
            str(opening_id): {"id": str(opening_id), "kind": "place", "name": "开局地点"},
            str(discovery_site_id): {"id": str(discovery_site_id), "kind": "place", "name": "发现地点"},
            str(player_id): {"id": str(player_id), "kind": "character", "name": "玩家", "location_id": str(opening_id)},
            str(witness_id): {"id": str(witness_id), "kind": "character", "name": "证人", "location_id": str(opening_id)},
            str(alternate_witness_id): {"id": str(alternate_witness_id), "kind": "character", "name": "另一位证人", "location_id": str(opening_id)},
        },
    })
    raw_state = base_state.model_dump(mode="json")
    raw_state["entities"][str(draft.anchor_id)] = {
        "id": str(draft.anchor_id), "kind": "item", "name": draft.anchor_name,
        "location_id": str(opening_id), "components": draft.anchor_components,
    }
    for item_id, name, location_id, components in draft.historical_items:
        raw_state["entities"][str(item_id)] = {
            "id": str(item_id), "kind": "item", "name": name,
            "location_id": str(location_id), "components": components,
        }
    raw_state["knowledge"].extend(draft.knowledge_records)
    WorldState.model_validate(raw_state)


def test_opening_case_has_distinct_safe_disclosure_labels_and_no_initial_alibi_leak() -> None:
    opening_id = uuid5(_NAMESPACE, "opening")
    discovery_site_id = uuid5(_NAMESPACE, "discovery")
    witness_id = uuid5(_NAMESPACE, "witness")
    alternate_witness_id = uuid5(_NAMESPACE, "alternate")

    draft = build_opening_case(
        "modern_city", "2.0.0", opening_id, discovery_site_id,
        witness_id, alternate_witness_id, _entity_id,
    )
    labels = [record["summary"] for record in draft.knowledge_records]
    assert len(labels) == len(set(labels)) == 2
    assert all(record["disclosure_policy"] == "if_asked" for record in draft.knowledge_records)
    assert all(record["visibility"]["policy"] == "private" for record in draft.knowledge_records)
    # The local item reveals only that it is a returned record copy; the typed
    # historical destination stays in the inspectable evidence component.
    alibi_item = next(item for item in draft.historical_items if item[0] == _entity_id("item", "opening-investigation-alibi-record"))
    assert alibi_item[2] == opening_id
    assert str(discovery_site_id) not in alibi_item[3]["public_description"]
    assert alibi_item[3]["historical_evidence"]["fact"]["value"] == str(discovery_site_id)
    assert "不能说明我后来是否听过或转述" in draft.knowledge_records[1]["claim"]


def test_opening_case_rejects_unknown_pack_and_duplicate_witness() -> None:
    ids = [uuid5(_NAMESPACE, value) for value in ("opening", "site", "witness", "alternate")]
    with pytest.raises(ValueError, match="No authored opening investigation"):
        build_opening_case("unshipped", "2.0.0", ids[0], ids[1], ids[2], ids[3], _entity_id)
    with pytest.raises(ValueError, match="two distinct named witnesses"):
        build_opening_case("modern_city", "2.0.0", ids[0], ids[1], ids[2], ids[2], _entity_id)
