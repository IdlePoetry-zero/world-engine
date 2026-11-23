"""Playable routes for the canon world pack. Travel times are package configuration."""

from uuid import UUID

from world_engine.domain.challenges import ChallengeDefinition
from world_engine.domain.components import PlaceComponent, PlaceRoute
from world_engine.domain.models import WorldEntity
from world_engine.domain.story_nodes import StoryReaction

from .gensokyo_reference import _id, build_geography_index
from .gensokyo_chronology import MORIYA_SURVEY_PERMIT


# Explicit walkable routes between atlas nodes without a source-backed parent.
_SURFACE_ROUTES = (
    ("human-village", "shrine", 45),
    ("human-village", "bamboo", 90),
    ("human-village", "kourindou", 35),
    ("kourindou", "forest", 20),
    ("forest", "shrine", 60),
    ("forest", "mayohiga", 45),
    ("human-village", "mansion-lakeshore", 60),
    ("mansion-lakeshore", "misty-lake", 15),
    ("mansion-lakeshore", "mansion", 15),
    ("mansion-lakeshore", "prismriver-house", 30),
    ("shrine", "hakurei-barrier", 15),
    ("forest", "youkai-mountain", 90),
    ("youkai-mountain", "moriya-shrine", 90),
    ("youkai-mountain", "triangular-mountain", 90),
    ("shrine", "shrine-geyser", 20),
    ("human-village", "myouren-temple", 35),
    ("myouren-temple", "temple-cemetery", 10),
    ("temple-cemetery", "yumedono", 20),
    ("shrine-geyser", "old-hell", 90),
    ("blazing-hell-ruins", "chireiden", 30),
    ("human-village", "sanzu-river", 90),
    ("yamanba-sanctuary", "kinjou-capital", 45),
)

# These links are conditional passage options, not canonical borders or distances.
# A character must obtain the corresponding capability before attempting a crossing.
_CROSSINGS = (
    ("hakurei-barrier", "outside-world", 30, "gensokyo.barrier_passage"),
    ("shrine", "netherworld", 60, "gensokyo.netherworld_passage"),
    ("shrine", "heaven", 90, "gensokyo.heaven_passage"),
    ("forest", "shining-needle-castle", 60, "gensokyo.sky_passage"),
    ("sanzu-river", "higan", 45, "gensokyo.sanzu_passage"),
    ("higan", "hell", 60, "gensokyo.hell_passage"),
    ("hell-checkpoint", "animal-realm", 60, "gensokyo.animal_realm_passage"),
    ("myouren-temple", "makai", 90, "gensokyo.makai_passage"),
    ("eientei", "dream-world", 75, "gensokyo.dream_passage"),
    ("dream-world", "lunar-capital", 75, "gensokyo.lunar_passage"),
)

_PRIVATE_GATES = {
    ("mansion-lakeshore", "mansion"): "gensokyo.mansion_guest",
    ("bamboo", "eientei"): "gensokyo.eientei_guest",
}
_PERMIT_TITLES = {
    "gensokyo.barrier_passage": "边界通行核验",
    "gensokyo.netherworld_passage": "核验冥途访客资格",
    "gensokyo.heaven_passage": "核验天界访客资格",
    "gensokyo.sky_passage": "空中航路登记",
    "gensokyo.sanzu_passage": "渡口来客登记",
    "gensokyo.hell_passage": "地狱访客登记",
    "gensokyo.animal_realm_passage": "核验关口访客资格",
    "gensokyo.makai_passage": "核验异界通行手续",
    "gensokyo.dream_passage": "梦境航路登记",
    "gensokyo.lunar_passage": "核验月都访客资格",
    "gensokyo.mansion_guest": "红魔馆来访登记",
    "gensokyo.eientei_guest": "竹林来访登记",
}


def register_passage(player_id: UUID, entities: dict[UUID, WorldEntity]) -> tuple[
    list[ChallengeDefinition], list[StoryReaction],
]:
    """Register a local application and award its permit only when the route's destination is available."""
    challenges = []
    reactions = []
    passages = [(start, end, permit, (permit,)) for start, end, _, permit in _CROSSINGS]
    passages.extend((start, end, permit, (permit, "place.enter_private")) for (start, end), permit in _PRIVATE_GATES.items())
    for start, end, permit, grants in passages:
        venue, destination = _id(f"place:{start}"), _id(f"place:{end}")
        if venue not in entities or destination not in entities:
            continue
        challenge_id = _id(f"world:passage:{permit}")
        title = _PERMIT_TITLES[permit]
        challenges.append(ChallengeDefinition(
            challenge_id=challenge_id, venue_id=venue, title=title,
            description="登记来访目的。目的地开始接待后，可使用此通行凭证。",
            skill_id="gensokyo.observation", skill_label="通行手续", pass_level="1",
        ))
        reactions.append(StoryReaction.model_validate({
            "reaction_id": _id(f"world:passage:{permit}:issued"),
            "when": [
                {"kind": "challenge_passed", "actor_id": player_id, "challenge_id": challenge_id},
                {"kind": "world_condition", "owner_id": destination, "condition": {
                    "kind": "entity_status", "subject_id": destination, "status": "active",
                }},
            ],
            "node": {
                "title": title, "visibility": "author_only", "participant_ids": [player_id],
                "fallback": {
                    "key": "permit-issued", "effect": "occurred", "summary": f"已核发通行凭证：{title}。",
                    "awards": [{"actor_id": player_id, "capability": capability} for capability in grants],
                },
            },
        }))
    return challenges, reactions


def connect_world_map(entities: dict[UUID, WorldEntity]) -> None:
    """Connect the places present at this opening through normal travel routes."""
    atlas_parents = tuple(
        (row["parent"], row["ref"], 20)
        for row in build_geography_index()
        if row["parent"] and row["kind"] != "organization"
    )
    links = [(start, end, minutes, ()) for start, end, minutes in (*_SURFACE_ROUTES, *atlas_parents)]
    links.extend((start, end, minutes, (access,)) for start, end, minutes, access in _CROSSINGS)
    links.append(("shrine", "moriya-shrine", 75, (MORIYA_SURVEY_PERMIT,)))
    routes: dict[UUID, dict[UUID, PlaceRoute]] = {}
    for first, second, minutes, access in links:
        first_id, second_id = _id(f"place:{first}"), _id(f"place:{second}")
        if first_id not in entities or second_id not in entities:
            continue
        for start, end in ((first_id, second_id), (second_id, first_id)):
            permit = _PRIVATE_GATES.get((first, second)) if start == first_id else None
            routes.setdefault(start, {})[end] = PlaceRoute(
                route_id=_id(f"world-path:{start}:{end}"),
                from_place_id=start,
                to_place_id=end,
                travel_ticks=minutes * 60,
                access_tags=[*access, *([permit] if permit else [])],
            )
    for place_id, destinations in routes.items():
        place = entities[place_id]
        place_data = PlaceComponent.model_validate(place.components["place"])
        # Existing scene entrances that have no atlas substitute are retained.
        existing = [route for route in place_data.routes if route.to_place_id not in destinations]
        updated = place_data.model_copy(update={"routes": [*existing, *destinations.values()]})
        entities[place_id] = place.model_copy(update={
            "components": place.components | {"place": updated.model_dump(mode="json")},
        })
