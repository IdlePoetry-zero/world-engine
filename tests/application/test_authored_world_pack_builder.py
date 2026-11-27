from __future__ import annotations

from copy import deepcopy
from uuid import NAMESPACE_URL, UUID, uuid5

import pytest

from world_engine.application.authored_world_pack_builder import (
    AuthoredWorldPackBuildError,
    AuthoredWorldPackBuilder,
    loads_authored_world_pack,
    minimal_authored_world_pack,
)
from world_engine.application.world_packs import load_world_pack


def _id(pack_id: str, opening_id: str, alias: str) -> str:
    return str(uuid5(NAMESPACE_URL, f"world-engine-authored:{pack_id}:{opening_id}:{alias}"))


def _scenario(document: dict) -> dict:
    template = document.get("template", document)
    return template["story_openings"][0]["scenario"]


def test_minimal_project_builds_deterministic_verified_archive_with_typed_route_references():
    document = minimal_authored_world_pack()
    first = AuthoredWorldPackBuilder().build(document)
    second = AuthoredWorldPackBuilder().build(document)

    assert first.archive == second.archive
    assert first.archive_sha256 == second.archive_sha256
    assert first.report["roundtrip_verified"] is True
    assert first.report["can_export"] is True

    reopened = load_world_pack(first.archive)
    scenario = reopened.template["story_openings"][0]["scenario"]
    traveler_id = _id("river-road-example", "first-step", "traveler")
    ferry_id = _id("river-road-example", "first-step", "ferry")
    north_bank_id = _id("river-road-example", "first-step", "north-bank")
    route_id = _id("river-road-example", "first-step", "north-bank-road")

    assert scenario["player_id"] == traveler_id
    assert scenario["entities"][traveler_id]["location_id"] == ferry_id
    route = scenario["entities"][ferry_id]["components"]["place"]["routes"][0]
    assert route == {
        "route_id": route_id,
        "from_place_id": ferry_id,
        "to_place_id": north_bank_id,
        "travel_ticks": 3600,
    }
    assert first.report["authored_ids"]["scenarios"][0]["entity_alias_count"] == 3
    assert first.report["authored_ids"]["scenarios"][0]["route_alias_count"] == 1


def test_alias_scope_changes_by_pack_and_opening_and_raw_templates_still_round_trip():
    source = minimal_authored_world_pack()
    first = AuthoredWorldPackBuilder().build(source)
    compiled = deepcopy(source)
    compiled["template"] = deepcopy(first.template)
    raw_build = AuthoredWorldPackBuilder().build(compiled)
    assert raw_build.archive == first.archive

    changed_pack = deepcopy(source)
    changed_pack["manifest"]["pack_id"] = "river-road-other"
    changed_pack_id = _id("river-road-other", "first-step", "traveler")
    changed_pack_scenario = _scenario(AuthoredWorldPackBuilder().build(changed_pack).template)
    assert changed_pack_id in changed_pack_scenario["entities"]

    changed_opening = deepcopy(source)
    changed_opening["template"]["story_openings"][0]["opening_id"] = "second-step"
    changed_opening_scenario = _scenario(AuthoredWorldPackBuilder().build(changed_opening).template)
    assert _id("river-road-example", "second-step", "traveler") in changed_opening_scenario["entities"]


def test_duplicate_entity_or_route_alias_is_rejected_before_pack_validation():
    duplicate_entity = minimal_authored_world_pack()
    duplicate_entity_scenario = _scenario(duplicate_entity)
    duplicate_entity_scenario["entities"].append(deepcopy(duplicate_entity_scenario["entities"][0]))
    with pytest.raises(AuthoredWorldPackBuildError, match="冲突"):
        AuthoredWorldPackBuilder().build(duplicate_entity)

    duplicate_route = minimal_authored_world_pack()
    routes = _scenario(duplicate_route)["entities"][1]["components"]["place"]["routes"]
    routes.append({"alias": "traveler", "to_place_id": {"$entity": "north-bank"}, "travel_ticks": 60})
    with pytest.raises(AuthoredWorldPackBuildError, match="冲突"):
        AuthoredWorldPackBuilder().build(duplicate_route)


def test_undefined_alias_wrong_reference_type_and_wrong_target_entity_kind_are_rejected():
    undefined = minimal_authored_world_pack()
    _scenario(undefined)["player_id"] = {"$entity": "missing-person"}
    with pytest.raises(AuthoredWorldPackBuildError, match="未定义实体 alias"):
        AuthoredWorldPackBuilder().build(undefined)

    wrong_marker = minimal_authored_world_pack()
    _scenario(wrong_marker)["player_id"] = {"$route": "north-bank-road"}
    with pytest.raises(AuthoredWorldPackBuildError, match=r"\$route 只能用于 route_id"):
        AuthoredWorldPackBuilder().build(wrong_marker)

    wrong_kind = minimal_authored_world_pack()
    ferry = _scenario(wrong_kind)["entities"][1]
    ferry["components"]["place"]["routes"][0]["to_place_id"] = {"$entity": "traveler"}
    with pytest.raises(AuthoredWorldPackBuildError, match="要求地点 alias"):
        AuthoredWorldPackBuilder().build(wrong_kind)

    route_as_entity = minimal_authored_world_pack()
    _scenario(route_as_entity)["player_id"] = {"$entity": "north-bank-road"}
    with pytest.raises(AuthoredWorldPackBuildError, match="未定义实体 alias"):
        AuthoredWorldPackBuilder().build(route_as_entity)


def test_route_origin_is_bound_to_containing_place():
    document = minimal_authored_world_pack()
    route = _scenario(document)["entities"][1]["components"]["place"]["routes"][0]
    route["from_place_id"] = {"$entity": "north-bank"}
    with pytest.raises(AuthoredWorldPackBuildError, match="包含该路线的地点"):
        AuthoredWorldPackBuilder().build(document)

    non_place_owner = minimal_authored_world_pack()
    scenario = _scenario(non_place_owner)
    traveler = scenario["entities"][0]
    traveler["components"]["place"] = {"routes": [{
        "alias": "invalid-route",
        "to_place_id": {"$entity": "north-bank"},
        "travel_ticks": 60,
    }]}
    with pytest.raises(AuthoredWorldPackBuildError, match="路线 alias 必须声明在地点实体上"):
        AuthoredWorldPackBuilder().build(non_place_owner)


def test_character_card_and_world_book_adapters_use_existing_mappers_and_report_dropped_card_fields():
    document = minimal_authored_world_pack()
    scenario = _scenario(document)
    scenario["character_cards"] = [{
        "alias": "keeper",
        "location_id": {"$entity": "ferry"},
        "card": {
            "spec": "chara_card_v2",
            "spec_version": "2.0",
            "data": {
                "name": "渡口掌柜",
                "description": "负责保管渡船。",
                "personality": "谨慎而直率",
                "scenario": "这里只作为未映射字段示例",
            },
        },
    }]
    scenario["world_book_adapter"] = {
        "visibility": "public",
        "source": {"entries": {
            "river_custom": {"title": "河规", "content": "涨水时暂停摆渡。", "keys": ["涨水"]},
            "disabled_note": {"content": "不导入。", "keys": ["废弃"], "enabled": False},
        }},
    }

    built = AuthoredWorldPackBuilder().build(document)
    scenario = load_world_pack(built.archive).template["story_openings"][0]["scenario"]
    keeper = scenario["entities"][_id("river-road-example", "first-step", "keeper")]
    assert keeper["name"] == "渡口掌柜"
    assert keeper["location_id"] == _id("river-road-example", "first-step", "ferry")
    assert keeper["components"]["personality"]["traits"] == ["谨慎而直率"]
    assert keeper["components"]["public_description"] == "负责保管渡船。"
    assert len(scenario["world_book_entries"]) == 1
    entry = scenario["world_book_entries"][0]
    assert entry["entry_id"] == _id("river-road-example", "first-step", "world-book:river_custom")
    assert entry["visibility"] == "public"
    adapter_report = built.report["authored_ids"]["scenarios"][0]
    assert adapter_report["character_cards"][0]["unmapped_fields"] == ["scenario"]
    assert adapter_report["world_book"]["mapped_entry_count"] == 1
    assert adapter_report["world_book"]["skipped_entries"][0]["reason"] == "disabled"


def test_json_loader_rejects_duplicate_properties_and_nonfinite_numbers():
    with pytest.raises(AuthoredWorldPackBuildError, match="duplicate JSON property"):
        loads_authored_world_pack('{"manifest":{},"manifest":{},"template":{}}')
    with pytest.raises(AuthoredWorldPackBuildError, match="non-finite JSON number"):
        loads_authored_world_pack('{"manifest":{"value":NaN},"template":{}}')
