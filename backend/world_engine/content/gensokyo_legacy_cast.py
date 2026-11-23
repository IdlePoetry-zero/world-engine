"""Adapt the author's recovered character cards to the story world."""
from __future__ import annotations

import json
from pathlib import Path

from world_engine.application.world_packs import WorldPackManifest, write_world_pack
from world_engine.domain.components import GoalRecord, PlaceComponent, PlaceRoute, RelationRecord, ScheduledEventRecord
from world_engine.domain.models import StoryArrival, WorldEntity, WorldState
from world_engine.domain.story_nodes import STORY_NODE_HANDLER, STORY_NODE_VERSION, StoryNode, StoryReaction

from .gensokyo_reference import PACK_ID, PACK_TITLE, _id, _scenario

CAST_VERSION = "0.7.0"
# These are scenario placements based on the author’s cards. Missing atlas
# locations and route distances are supplied by this playable map.
_VENUES = {
    "hakurei_shrine": ("shrine", "博丽神社", "human-village"),
    "magic_forest": ("forest", "魔法森林", "shrine"),
    "human_village": ("human-village", "人间之里", "shrine"),
    "misty_lake": ("misty-lake", "雾之湖", "mansion-lakeshore"),
    "scarlet_devil_mansion_gate": ("mansion-lakeshore", "红魔馆外湖岸", "human-village"),
    "scarlet_devil_mansion_interior": ("mansion", "红魔馆", "mansion-lakeshore"),
    "eientei": ("eientei", "永远亭", "bamboo"),
    "bamboo_forest_of_the_lost": ("bamboo", "迷途竹林", "human-village"),
    "beast_path": ("beast-path", "兽道", "human-village"),
    "kourindou": ("kourindou", "香霖堂", "forest"),
    "mayohiga": ("mayohiga", "迷途之家", "forest"),
    "hakugyokurou": ("saigyouji-residence", "白玉楼", "shrine"),
    "youkai_mountain": ("youkai-mountain", "妖怪之山", "forest"),
    "sunflower_field": ("sunflower-field", "太阳花田", "human-village"),
}
# A later publication is an evidence boundary for this scenario, not a birth date.
_LATER_PROFILE = {"keine": 8, "mokou": 8, "yuuka": 9}
_CORE_TRAITS = {
    "reimu": ["待人平等", "日常悠闲", "遇到异变会主动调查"],
    "marisa": ["好胜直率", "勤于钻研魔法", "收集魔法道具"],
    "yukari": ["行踪难测", "嫌麻烦", "操纵境界并役使式神"],
    "flandre": ["长期在馆内生活", "好奇外界"],
    "koakuma": ["协助处理馆内书籍"],
}
_OPENINGS = (
    ("scarlet-mist", "红雾出现后", "红雾已影响日照，世界内日期未定。", 6, ("scarlet-mist",)),
    ("false-moon", "满月异变之夜", "异常月亮已出现，黎明之前。", 8, ("scarlet-mist", "late-spring", "false-moon")),
    ("mountain-shrine", "山上新神社出现之后", "新的山上神社已出现，世界内日期未定。", 10,
     ("scarlet-mist", "late-spring", "false-moon", "mountain-shrine")),
)


def _add_link(entities: dict, start_key: str, end_key: str) -> None:
    for origin, destination in ((start_key, end_key), (end_key, start_key)):
        source_id, target_id = _id(f"place:{origin}"), _id(f"place:{destination}")
        if source_id not in entities or target_id not in entities:
            continue
        source = entities[source_id]
        place = PlaceComponent.model_validate(source.components["place"])
        if any(route.to_place_id == target_id for route in place.routes):
            continue
        route = PlaceRoute(route_id=_id(f"cast:route:{origin}:{destination}"), from_place_id=source_id,
                           to_place_id=target_id, travel_ticks=180)
        entities[source_id] = source.model_copy(update={
            "components": source.components | {"place": place.model_copy(
                update={"routes": [*place.routes, route]}).model_dump(mode="json")},
        })


def _build_opening(cards: list[dict], incident: str, version: int, known_incidents: tuple[str, ...]) -> WorldState:
    state = WorldState.model_validate(_scenario(
        incident=incident, document_limit=version, known_incidents=frozenset(known_incidents),
        description="沿旧作人物配置探索。角色的场景位置及日程属于本包游玩安排。",
    ))
    entities = dict(state.entities)
    arrivals = list(state.story_arrivals)
    schedules = list(state.scheduled_events)
    relations = list(state.relations)
    reactions = list(state.story_reactions)
    for card in cards:
        _, key, publication = card["card_id"].split("_")
        if max(int(publication[2:]), _LATER_PROFILE.get(key, 0)) > version:
            continue
        place_key, place_name, adjacent = _VENUES[card["bind_node"]]
        place_id = _id(f"place:{place_key}")
        entities.setdefault(place_id, WorldEntity(id=place_id, kind="place", name=place_name,
                                                  components={"place": {"place_type": "site"}}))
        _add_link(entities, place_key, adjacent)
        role_id = _id(f"legacy-cast:{key}")
        # Transfer authored portrayal guidance as descriptive fields only.
        # Legacy numeric bonuses, endings, and forced actions stay outside the rules.
        traits = _CORE_TRAITS.get(key) or [line[:160] for line in card.get("persona_rules", ())
                                          if line and "一设" in line and "二设" not in line][:2]
        name = card["name"].split(" (", 1)[0].strip()
        actor = WorldEntity(id=role_id, kind="character", name=name, location_id=place_id, components={
            "personality": {"traits": traits, "locked_fields": ["traits"]},
            "capabilities": {"grants": ["world.travel"]}, "known_places": [str(place_id)],
        })
        # Reimu's field check uses a destination already present in the author's
        # old routine data. The exact appointment is a scenario choice.
        if key == "reimu":
            village = _id("place:human-village")
            actor = actor.model_copy(update={"components": actor.components | {"known_places": [str(place_id), str(village)]}})
            entrance = _id(f"{incident}:reimu-cast-arrival")
            schedules.append(ScheduledEventRecord(
                id=entrance, due_tick=0, phase="observation", handler_id=STORY_NODE_HANDLER,
                handler_version=STORY_NODE_VERSION, payload=StoryNode.model_validate({
                    "title": "博丽神社开始值守", "visibility": "author_only", "participant_ids": [role_id],
                    "fallback": {"key": "present", "effect": "occurred", "summary": "巫女在神社开始值守。"},
                }).model_dump(mode="json"),
            ))
            arrivals.append(StoryArrival(trigger_id=entrance, actor=actor, review_delay_ticks=900, goals=(GoalRecord(
                id=_id(f"{incident}:reimu-village-check"), owner_id=role_id, title="到人间之里巡查",
                description="到人间之里查访当前异变对居民的影响。", auto_complete=True,
                conditions=[{"kind": "reach_location", "subject_id": role_id, "target_id": village}],
            ),)))
            for suffix, title, condition, summary in (
                ("contact", "神社接到外勤消息", {"kind": "relationship_dimension_minimum", "subject_id": state.player_id,
                    "target_id": role_id, "dimension": "familiarity", "threshold": "1"}, "外勤观测员与博丽灵梦建立了工作联系。"),
                ("patrol", "巫女抵达人间之里", {"kind": "reach_location", "subject_id": role_id, "target_id": village},
                 "博丽灵梦抵达人间之里，巡查开始。"),
            ):
                reactions.append(StoryReaction.model_validate({
                    "reaction_id": _id(f"{incident}:reimu:{suffix}"),
                    "when": [{"kind": "world_condition", "owner_id": condition["subject_id"], "condition": condition}],
                    "node": {"title": title, "visibility": "public", "participant_ids": [role_id],
                             "fallback": {"key": "occurred", "effect": "occurred", "summary": summary}},
                }))
        else:
            entities[role_id] = actor

    for leader, companion, label in (("yukari", "ran", "役使式神"), ("eirin", "kaguya", "共同居住于永远亭")):
        first, second = _id(f"legacy-cast:{leader}"), _id(f"legacy-cast:{companion}")
        if first in entities and second in entities:
            relations.append(RelationRecord(id=_id(f"legacy-cast:relation:{leader}:{companion}"),
                                            from_id=first, to_id=second, label=label))
    player = entities[state.player_id]
    capabilities = player.components["capabilities"]
    entities[player.id] = player.model_copy(update={"components": player.components | {
        "capabilities": capabilities | {"grants": [*capabilities["grants"], "place.enter_private"]},
        "public_role": "获红魔馆与永远亭访客许可的人里观测员",
    }})
    return WorldState.model_validate(state.model_dump(mode="python") | {
        "entities": entities, "story_arrivals": arrivals, "scheduled_events": schedules, "relations": relations,
        "story_reactions": reactions, "metadata": state.metadata | {"pack_version": CAST_VERSION},
    })


def build_cast_archive(card_directory: Path) -> bytes:
    cards = [json.loads(path.read_text(encoding="utf-8-sig")) for path in sorted(card_directory.glob("char_*.json"))]
    if not cards or len({card["card_id"] for card in cards}) != len(cards):
        raise ValueError("The directory must contain distinct recovered character cards.")
    return write_world_pack(manifest=WorldPackManifest(pack_id=PACK_ID, version=CAST_VERSION, title=PACK_TITLE),
                            template={"story_openings": [
        {"opening_id": incident, "title": title, "position": position, "known_history": [position],
         "scenario": _build_opening(cards, incident, limit, history).model_dump(mode="json")}
        for incident, title, position, limit, history in _OPENINGS
    ]})
