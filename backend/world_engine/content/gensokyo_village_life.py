"""Local goods and public stores that can be affected by the authored investigations."""

from dataclasses import dataclass
from uuid import UUID

from world_engine.domain.components import MarketComponent, MarketOffer
from world_engine.domain.models import ResourceAccount, WorldEntity
from world_engine.domain.story_nodes import StoryTransfer

from .gensokyo_reference import _id


_CURRENCY = "gensokyo.copper"
_RELIEF = "gensokyo.relief_ration"
_SUPPLIES = "gensokyo.care_supply"
RESOURCE_LABELS = {
    _CURRENCY: "铜钱",
    "gensokyo.rice": "米粮",
    "gensokyo.tea": "茶叶",
    "gensokyo.tool_parts": "旧器件",
    "gensokyo.lamp_oil": "灯油",
    _RELIEF: "公用救济口粮",
    _SUPPLIES: "公用照护物资",
}
_COUNTERS = {
    "human-village": ("人里粮摊", (("gensokyo.rice", 24, 3), ("gensokyo.tea", 12, 2))),
    "kourindou": ("香霖堂寄售柜", (("gensokyo.tool_parts", 8, 4), ("gensokyo.lamp_oil", 10, 3))),
}
_STORES = {
    ("human-village", _RELIEF): 18,
    ("shrine", _RELIEF): 0,
    ("moriya-shrine", _RELIEF): 0,
    ("human-village", _SUPPLIES): 8,
    ("shrine", _SUPPLIES): 1,
    ("eientei", _SUPPLIES): 4,
}
_AID = {
    "scarlet-mist": ("human-village", "shrine", _RELIEF, 6, 60, "六份救济口粮交由博丽神社保管，可调给受红雾影响的访客。"),
    "mountain-shrine": ("human-village", "moriya-shrine", _RELIEF, 4, 255, "四份口粮拨给守矢神社，供上山或滞留的来客使用。"),
    "geyser-spirits": ("human-village", "shrine", _SUPPLIES, 3, 75, "三份照护物资拨往博丽神社，供间歇泉附近的临时避护使用。"),
    "lunar-envoys": ("human-village", "eientei", _SUPPLIES, 4, 150, "四份照护物资拨往永远亭，作为应对地面来访者所需的备品。"),
}


@dataclass(frozen=True)
class LocalAid:
    origin_id: UUID
    destination_id: UUID
    delay_ticks: int
    transfer: StoryTransfer
    summary: str


def _account(owner: UUID, resource: str) -> UUID:
    return _id(f"world:stock:{owner}:{resource}")


def add_local_economy(entities: dict[UUID, WorldEntity], player_id: UUID) -> dict[UUID, ResourceAccount]:
    resources = {}

    def hold(owner: UUID, resource: str, amount: int) -> UUID:
        ident = _account(owner, resource)
        resources[ident] = ResourceAccount(resource_id=resource, balance=amount, owner_id=owner)
        return ident

    hold(player_id, _CURRENCY, 32)
    for (place, resource), amount in _STORES.items():
        hold(_id(f"place:{place}"), resource, amount)
    for place, (name, goods) in _COUNTERS.items():
        venue = entities[_id(f"place:{place}")]
        seller = _id(f"world:counter:{place}")
        hold(seller, _CURRENCY, 0)
        offers = []
        lots = []
        for resource, count, price in goods:
            stock = hold(seller, resource, count)
            offers.append(MarketOffer(
                listing_id=_id(f"world:listing:{place}:{resource}"), seller_id=seller,
                resource_id=resource, stock_account_id=stock, unit_price_minor=price,
            ))
            lots.append({
                "lot_id": str(_id(f"world:counter-lot:{place}:{resource}")), "account_id": str(stock),
                "resource_id": resource, "quantity": str(count),
            })
        entities[seller] = WorldEntity(
            id=seller, kind="organization", name=name, location_id=venue.id,
            components={"inventory": {"lots": lots}, "public_role": "本世界包配置的寄售货柜"},
        )
        entities[venue.id] = venue.model_copy(update={"components": venue.components | {
            "market": MarketComponent(currency_resource_id=_CURRENCY, offers=offers).model_dump(mode="json"),
        }})
    for resource in RESOURCE_LABELS:
        total = sum(account.balance for account in resources.values() if account.resource_id == resource)
        resources[_id(f"world:stock:issue:{resource}")] = ResourceAccount(
            resource_id=resource, balance=-total, account_type="issuance_source",
        )
    return resources


def local_aid(chapter_id: str) -> LocalAid | None:
    if (response := _AID.get(chapter_id)) is None:
        return None
    start, end, resource, quantity, minutes, summary = response
    origin = _id(f"place:{start}")
    destination = _id(f"place:{end}")
    return LocalAid(origin, destination, minutes * 60, StoryTransfer(
        from_account_id=_account(origin, resource), to_account_id=_account(destination, resource),
        resource_id=resource, quantity=quantity,
    ), summary)
