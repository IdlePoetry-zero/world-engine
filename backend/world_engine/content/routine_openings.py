"""Local, recurring visits for the opt-in routine catalog version."""

from dataclasses import dataclass
from typing import Any
from uuid import UUID, uuid5


ROUTINE_OPENING_PACK_VERSION = "2.12.0"

_VISIT_START = 8 * 3_600
_RETURN_START = 12 * 3_600 + 30 * 60


@dataclass(frozen=True, slots=True)
class RoutineOpening:
    role: str
    purpose: str
    weather: str
    walk_seconds: int


ROUTINE_OPENINGS = {
    "low_magic_feudal": RoutineOpening("bridge reeve", "核对过桥开支并听取往来者对于修桥摊派的陈述", "clear", 900),
    "modern_city": RoutineOpening("night rail dispatcher", "复核末班换乘，收集乘客前一夜的改道记录", "clear", 600),
    "long_life_cultivation": RoutineOpening("sect archivist", "对勘分水轮次抄本，记录各方带来的旧账", "storm", 1_200),
}


def prepare_routine_opening(
    opening: RoutineOpening,
    actor: dict[str, Any],
    primary: dict[str, Any],
    home: dict[str, Any],
    fallback: dict[str, Any],
) -> str:
    """Add one real round trip to either of two visible meeting sites."""
    actor_id = UUID(actor["id"])
    visit = str(uuid5(actor_id, "daily:routine-opening:public-visit"))
    rest = str(uuid5(actor_id, "daily:routine-opening:midday-rest"))
    primary["components"]["place"]["environment"]["weather"] = opening.weather
    for destination in (primary, fallback):
        _local_walk(home, destination, opening.walk_seconds)
        _local_walk(destination, home, opening.walk_seconds)

    components = actor["components"]
    components["daily_routine"] = {"windows": [
        {"window_id": visit, "start_second": _VISIT_START, "duration_seconds": 7_200,
         "activity": "socialize", "place_id": primary["id"]},
        {"window_id": rest, "start_second": _RETURN_START, "duration_seconds": 3_600,
         "activity": "rest", "place_id": home["id"]},
    ]}
    components["routine_mobility"] = {"avoid_conditions": ["storm"], "alternatives": {visit: [fallback["id"]]}}
    components["known_places"].extend((primary["id"], fallback["id"]))
    return (
        f"{actor['name']} 每日 08:00 到 {primary['name']}{opening.purpose}。"
        f"这里遇暴风时改在 {fallback['name']} 相见，12:30 返回 {home['name']}。"
    )


def _local_walk(origin: dict[str, Any], destination: dict[str, Any], seconds: int) -> None:
    routes = origin["components"]["place"]["routes"]
    route = next((item for item in routes if item["to_place_id"] == destination["id"]), None)
    if route is None:
        route = {
            "route_id": str(uuid5(UUID(origin["id"]), f"routine-opening:walk:{destination['id']}")),
            "from_place_id": origin["id"],
            "to_place_id": destination["id"],
            "capacity": 500,
            "access_tags": [],
            "travel_modes": ["travel.walk"],
            "is_open": True,
        }
        routes.append(route)
    route.update(travel_ticks=seconds, risk="0")
