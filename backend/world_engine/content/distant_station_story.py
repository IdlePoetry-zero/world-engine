"""Original science-fiction example of intervention and a fixed outside event."""

from decimal import Decimal
from uuid import UUID, uuid5

from world_engine.application.world_packs import WorldPackManifest, write_world_pack
from world_engine.domain.components import PlaceComponent, PlaceRoute, ScheduledEventRecord
from world_engine.domain.models import ResourceAccount, WorldEntity, WorldState
from world_engine.domain.story_nodes import STORY_NODE_HANDLER, STORY_NODE_VERSION, StoryNode, StoryReaction


PACK_ID = "author.distant-station"
PACK_VERSION = "1.0.0"
PACK_TITLE = "远岬站·脉冲将至"
_NAMESPACE = UUID("58995de2-b5a0-45cd-ab6e-b4e52aa46922")
_RESOURCE = "station.power_cells"


def _id(label: str) -> UUID:
    return uuid5(_NAMESPACE, label)


def _opening(*, tick: int, at_shelter: bool) -> WorldState:
    player, landing, shelter, operator = map(_id, ("player", "landing", "shelter", "operator"))
    stock, reserve = _id("main-stock"), _id("shelter-reserve")
    supply_reaction, report_reaction = _id("shelter-supply"), _id("supply-report")
    pulse = _id("stellar-pulse")

    def site(ident, name, destination):
        route = PlaceRoute(route_id=_id(f"route:{ident}"), from_place_id=ident, to_place_id=destination, travel_ticks=60)
        return WorldEntity(id=ident, name=name, kind="place", components={
            "place": PlaceComponent(place_type="building", routes=[route]).model_dump(mode="json"),
        })

    supply_confirmed = {"kind": "prior_outcome", "schedule_id": supply_reaction, "outcome": "supplied"}
    power_received = {"kind": "world_condition", "owner_id": shelter, "condition": {
        "kind": "resource_balance_minimum", "subject_id": shelter, "resource_id": _RESOURCE, "quantity": "3",
    }}
    return WorldState(
        player_id=player, current_tick=tick,
        entities={
            landing: site(landing, "接驳台", shelter), shelter: site(shelter, "地下避护区", landing),
            operator: WorldEntity(id=operator, name="值守员伊莘", kind="character", location_id=shelter,
                                  components={"public_role": "避护区值守员"}),
            player: WorldEntity(id=player, name="外勤员", kind="character", location_id=shelter if at_shelter else landing,
                                 components={"known_places": [str(landing), str(shelter)],
                                             "capabilities": {"grants": ["world.travel", "station.reserve.accept"]}}),
        },
        resources={
            stock: ResourceAccount(owner_id=landing, resource_id=_RESOURCE, balance=Decimal("3")),
            reserve: ResourceAccount(owner_id=shelter, resource_id=_RESOURCE, balance=Decimal("0")),
        },
        story_reactions=[StoryReaction.model_validate({
            "reaction_id": supply_reaction,
            "when": [{"kind": "world_condition", "owner_id": player,
                      "condition": {"kind": "reach_location", "subject_id": player, "target_id": shelter}},
                     {"kind": "capability", "actor_id": player, "capability": "station.reserve.accept"}],
            "node": {"title": "接收备用电池", "visibility": "public", "participant_ids": [player, operator],
                     "variants": [{"when": [{"kind": "world_condition", "owner_id": landing, "condition": {
                         "kind": "resource_balance_minimum", "subject_id": landing, "resource_id": _RESOURCE, "quantity": "3",
                     }}], "outcome": {"key": "supplied", "effect": "mitigated", "summary": "避护区验收并收到三块备用电池。",
                                      "transfer": {"from_account_id": stock, "to_account_id": reserve,
                                                   "resource_id": _RESOURCE, "quantity": "3"}}}],
                     "fallback": {"key": "stock-unavailable", "effect": "inapplicable", "summary": "接驳台备用库存不足。"}},
        }), StoryReaction.model_validate({
            "reaction_id": report_reaction,
            "when": [supply_confirmed, power_received],
            "node": {"title": "避护准备报告", "visibility": "public", "participant_ids": [operator],
                     "fallback": {"key": "ready", "effect": "occurred", "summary": "值守员确认应急电源已入账。"}},
        })],
        scheduled_events=[ScheduledEventRecord(
            id=pulse, due_tick=180, phase="observation", handler_id=STORY_NODE_HANDLER,
            handler_version=STORY_NODE_VERSION, payload=StoryNode.model_validate({
                "title": "恒星脉冲抵达", "visibility": "public",
                "variants": [{"when": [power_received], "outcome": {
                    "key": "sheltered", "effect": "mitigated", "summary": "恒星脉冲照常抵达，避护区凭应急电源保持照明。",
                }}], "fallback": {"key": "pulse", "effect": "occurred", "summary": "恒星脉冲照常抵达，避护区切到无照明值守。"},
            }).model_dump(mode="json"),
        )],
        metadata={"pack_id": PACK_ID, "pack_version": PACK_VERSION, "public_title": PACK_TITLE,
                  "public_description": "仪器预测恒星脉冲即将抵达。你负责到地下避护区验收备用电池。"},
    )


def build_archive() -> bytes:
    return write_world_pack(
        manifest=WorldPackManifest(pack_id=PACK_ID, version=PACK_VERSION, title=PACK_TITLE),
        template={"story_openings": [
            {"opening_id": "arrival", "title": "抵达接驳台", "position": "脉冲抵达前三分钟。",
             "known_history": ["远岬站的仪器已发出预警。"], "scenario": _opening(tick=0, at_shelter=False).model_dump(mode="json")},
            {"opening_id": "shelter-shift", "title": "进入避护区值班", "position": "脉冲抵达前一分钟。",
             "known_history": ["预警已传达，备用电池仍在接驳台待验收。"],
             "scenario": _opening(tick=120, at_shelter=True).model_dump(mode="json")},
        ]},
    )
