"""Deterministic, validated content packs for new worlds.

The catalog writes only a starting snapshot. It does not claim that any of the
history, movement, production, or social change implied by the pack rules has
already been simulated.
"""

from __future__ import annotations

import hashlib
from dataclasses import replace
from decimal import Decimal
from itertools import product
from typing import Any, Literal
from uuid import UUID, uuid5

from world_engine.domain.market_pricing import (
    PRICE_REVIEW_CONFIG_KEY, PRICE_REVIEW_CONFIG_VERSION, PRICE_REVIEW_DAYS,
    PRICE_REVIEW_HANDLER_ID, PRICE_REVIEW_HANDLER_VERSION,
)
from world_engine.domain.models import NewWorld, WorldState
from world_engine.domain.population_seats import select_population_occupation_role
from world_engine.domain.promise_trust_settlement import PROMISE_TRUST_POLICY_KEY
from world_engine.domain.relationship_promises import PROMISE_TRUST_POLICY_ID

from .definitions import (
    BACKGROUNDS,
    OPENING_INVESTIGATIONS,
    BackgroundDefinition,
    PersonSeed,
)
from .cultivation_occupation_profiles_211 import (
    CULTIVATION_OCCUPATION_PROFILES_211_VERSION,
    build_cultivation_occupation_profiles_211,
)
from .feudal_occupation_profiles_290 import (
    FEUDAL_ORGANIZATION_HOME_REGIONS_290,
    build_feudal_occupation_profiles_290,
    validate_feudal_occupation_contract_290,
)
from .investigation_openings import build_opening_case
from .modern_occupation_profiles_260 import (
    MODERN_OCCUPATION_PROFILES_260_VERSION,
    build_modern_city_occupation_profiles_260,
    validate_modern_city_occupation_contract_260,
)
from .recruitment_roles import RECRUITMENT_ROLES
from .routine_openings import ROUTINE_OPENING_PACK_VERSION, ROUTINE_OPENINGS, RoutineOpening, prepare_routine_opening
from .strategy_examples import reserve_strategy
from .waypoint_openings import build_waypoint_openings, waypoint_opening_notice


_CONTENT_NAMESPACE = UUID("6b88bfc8-6719-4a08-8b48-8a8c8f425087")
_RULESET_VERSION = "1.0.0"
_SUSTAINED_PACK_VERSION = "1.8.0"
_INVESTIGATION_PACK_VERSION = "1.9.0"
_CASE_PACK_VERSION = "2.0.0"
_MARKET_PACK_VERSION = "2.1.0"
_TRUST_PACK_VERSION = "2.2.0"
_FAMILY_PACK_VERSION = "2.3.0"
_DENSE_SUSTAINED_PACK_VERSION = "2.4.0"
_MODERN_OCCUPATION_PACK_VERSION = "2.5.0"
_MODERN_OCCUPATION_260_PACK_VERSION = MODERN_OCCUPATION_PROFILES_260_VERSION
_MODERN_CIVIC_OCCUPATION_PACK_VERSION = "2.7.0"
_WAYPOINT_OPENING_PACK_VERSION = "2.8.0"
_FEUDAL_OCCUPATION_PACK_VERSION_290 = "2.9.0"
_MODERN_CIVIC_RECRUITMENT_PACK_VERSION_210 = "2.10.0"
_CULTIVATION_OCCUPATION_PACK_VERSION_211 = CULTIVATION_OCCUPATION_PROFILES_211_VERSION
_ORGANIZATION_STRATEGY_PACK_VERSION = "2.13.0"
_PUBLIC_WAYPOINT_PACK_VERSIONS = {
    _WAYPOINT_OPENING_PACK_VERSION, ROUTINE_OPENING_PACK_VERSION, _ORGANIZATION_STRATEGY_PACK_VERSION,
}
_MODERN_OCCUPATION_PROFILE_VERSIONS = {
    _MODERN_OCCUPATION_PACK_VERSION, _MODERN_OCCUPATION_260_PACK_VERSION, _MODERN_CIVIC_OCCUPATION_PACK_VERSION,
}
_FAMILY_PACK_VERSIONS = {
    _FAMILY_PACK_VERSION, _DENSE_SUSTAINED_PACK_VERSION, *_PUBLIC_WAYPOINT_PACK_VERSIONS,
    _FEUDAL_OCCUPATION_PACK_VERSION_290, _MODERN_CIVIC_RECRUITMENT_PACK_VERSION_210,
    _CULTIVATION_OCCUPATION_PACK_VERSION_211,
    *_MODERN_OCCUPATION_PROFILE_VERSIONS,
}
_CASE_PACK_VERSIONS = {
    _CASE_PACK_VERSION, _MARKET_PACK_VERSION, _TRUST_PACK_VERSION, *_FAMILY_PACK_VERSIONS,
}
_PRICING_PACK_VERSIONS = {
    _MARKET_PACK_VERSION, _TRUST_PACK_VERSION, *_FAMILY_PACK_VERSIONS,
}
_DAY_SECONDS = 86_400
_HOUR_SECONDS = 3_600
_KEY_PEOPLE = 32
_OTHER_RESIDENTS = 240
_DENSE_SUSTAINED_RESIDENTS = 264
_REGION_COUNT = 12
_SETTLEMENTS_PER_REGION = 9
_POLITY_COUNT = 24
_ORGANIZATION_COUNT = 24

_OPENING_DESCRIPTIONS = {
    "modern_city": ("现代都市", "在街区、车站和工作场所之间来往。通勤、工作安排、公共设施和人物关系都会影响一天怎么过。"),
    "low_magic_feudal": ("低魔封建", "河谷领地之间有桥税、行路许可和集镇贸易。可以走访证人、打听道路或专注自己的旅程。"),
    "long_life_cultivation": ("长生修行", "将修行、宗门往来和山川旅行安排在同一条时间线上。境界突破有各自的条件和代价。"),
}

_OPENING_CHOICES = {
    "modern_city": (
        {"title": "追查末班车", "description": "到南线站核对停运影响，决定找人问话、查记录或继续自己的安排。",
         "version": _CASE_PACK_VERSION},
        {"title": "约在下一班之前", "description": "按调度员的时间和地点去见面，也可把时间用在工作或外出。暴风时地点会调整。",
         "version": ROUTINE_OPENING_PACK_VERSION},
        {"title": "机构自治", "description": "地区机构会依据自身章程、人员和账本定期调整储备。",
         "version": _ORGANIZATION_STRATEGY_PACK_VERSION},
    ),
    "low_magic_feudal": (
        {"title": "沿灰境林道出发", "description": "规划多段旅程。抵达林道驿点后，可赶路，也可停留核对过路抄表。",
         "version": _WAYPOINT_OPENING_PACK_VERSION},
        {"title": "找桥吏谈修桥摊派", "description": "从当地告示找到接待时段。你也可以等下一次会面，或先走自己的路。",
         "version": ROUTINE_OPENING_PACK_VERSION},
    ),
}


def _named_resident_capacity(version: str | None, population_mode: str | None) -> int:
    if version in {
        *_PUBLIC_WAYPOINT_PACK_VERSIONS, _FEUDAL_OCCUPATION_PACK_VERSION_290,
        _MODERN_CIVIC_RECRUITMENT_PACK_VERSION_210,
        _CULTIVATION_OCCUPATION_PACK_VERSION_211,
    }:
        return _DENSE_SUSTAINED_RESIDENTS if population_mode == "sustained" else _OTHER_RESIDENTS
    if version in {_DENSE_SUSTAINED_PACK_VERSION, *_MODERN_OCCUPATION_PROFILE_VERSIONS}:
        return _DENSE_SUSTAINED_RESIDENTS
    return _OTHER_RESIDENTS


class ContentCatalog:
    """Default full-capacity catalog implementing ``application.worlds.PackCatalog``."""

    def __init__(self) -> None:
        self._by_id = {background.pack_id: background for background in BACKGROUNDS}

    def list_packs(self) -> list[dict[str, Any]]:
        """Return safe, JSON-ready pack metadata for the world-creation UI."""
        return [
            {
                "pack_id": pack.pack_id,
                "version": _CASE_PACK_VERSION,
                "available_versions": [
                    _CASE_PACK_VERSION, _TRUST_PACK_VERSION, _FAMILY_PACK_VERSION, _WAYPOINT_OPENING_PACK_VERSION,
                    ROUTINE_OPENING_PACK_VERSION, _ORGANIZATION_STRATEGY_PACK_VERSION,
                ],
                "sustained_available_versions": (
                    [
                        _DENSE_SUSTAINED_PACK_VERSION,
                        _MODERN_OCCUPATION_PACK_VERSION,
                        _MODERN_OCCUPATION_260_PACK_VERSION,
                        _MODERN_CIVIC_OCCUPATION_PACK_VERSION,
                        _MODERN_CIVIC_RECRUITMENT_PACK_VERSION_210,
                    ]
                    if pack.pack_id == "modern_city"
                    else (
                        [_DENSE_SUSTAINED_PACK_VERSION, _FEUDAL_OCCUPATION_PACK_VERSION_290]
                        if pack.pack_id == "low_magic_feudal"
                        else (
                            [_DENSE_SUSTAINED_PACK_VERSION, _CULTIVATION_OCCUPATION_PACK_VERSION_211]
                            if pack.pack_id == "long_life_cultivation"
                            else [_DENSE_SUSTAINED_PACK_VERSION]
                        )
                    )
                ),
                "title": pack.title,
                "subtitle": _OPENING_DESCRIPTIONS[pack.pack_id][0],
                "summary": _OPENING_DESCRIPTIONS[pack.pack_id][1],
                **({"opening_choices": [dict(item) for item in choices]}
                   if (choices := _OPENING_CHOICES.get(pack.pack_id)) else {}),
                "themes": list(pack.themes),
                "systems": list(pack.systems),
                "capacity": {
                    "countries": _POLITY_COUNT,
                    "organizations": _ORGANIZATION_COUNT,
                    "key_npcs": _KEY_PEOPLE,
                    "other_named_living_characters": _OTHER_RESIDENTS,
                    "places": _REGION_COUNT * (1 + _SETTLEMENTS_PER_REGION),
                },
            }
            for pack in BACKGROUNDS
        ]

    def build_world(
        self,
        pack_id: str,
        title: str,
        player_name: str,
        *,
        seed: int | None = None,
        population_mode: Literal["natural", "sustained"] = "natural",
        pack_version: str | None = None,
    ) -> NewWorld:
        """Create a stable, fully cross-referenced world draft from a pack."""
        try:
            pack = self._by_id[pack_id]
        except (KeyError, TypeError) as exc:
            raise ValueError(f"Unknown background pack: {pack_id!r}.") from exc

        world_title = _required_text(title, "title", 240)
        player_display_name = _required_text(player_name, "player_name", 240)
        if seed is not None and (type(seed) is not int or not -(2**63) <= seed <= 2**63 - 1):
            raise ValueError("seed must be a signed 64-bit integer or None.")
        if population_mode not in {"natural", "sustained"}:
            raise ValueError("population_mode must be 'natural' or 'sustained'.")
        if pack_version in {None, _CASE_PACK_VERSION}:
            pack = replace(pack, version=_CASE_PACK_VERSION)
        elif pack_version == _DENSE_SUSTAINED_PACK_VERSION:
            if population_mode != "sustained":
                raise ValueError("Pack version 2.4.0 is supported only with sustained population mode.")
            pack = replace(pack, version=_DENSE_SUSTAINED_PACK_VERSION)
        elif pack_version == _MODERN_OCCUPATION_PACK_VERSION:
            if population_mode != "sustained":
                raise ValueError("Pack version 2.5.0 is supported only with sustained population mode.")
            if pack_id != "modern_city":
                raise ValueError("Pack version 2.5.0 is supported only for the modern_city background.")
            if set(pack.regional_occupation_profiles or {}) != {region.name for region in pack.regions}:
                raise ValueError("Pack version 2.5.0 requires a regional occupation profile for every region.")
            pack = replace(pack, version=_MODERN_OCCUPATION_PACK_VERSION)
        elif pack_version == _MODERN_CIVIC_RECRUITMENT_PACK_VERSION_210:
            if population_mode != "sustained":
                raise ValueError("Pack version 2.10.0 is supported only with sustained population mode.")
            if pack_id != "modern_city":
                raise ValueError("Pack version 2.10.0 is supported only for the modern_city background.")
            profiles = build_modern_city_occupation_profiles_260()
            validate_modern_city_occupation_contract_260(
                regions=pack.regions,
                organizations=pack.organizations,
                profiles=profiles,
                roles=pack.extra_roles,
                recruitment_roles=RECRUITMENT_ROLES[pack_id],
            )
            pack = replace(
                pack,
                version=_MODERN_CIVIC_RECRUITMENT_PACK_VERSION_210,
                regional_occupation_profiles=profiles,
            )
        elif pack_version in {_MODERN_OCCUPATION_260_PACK_VERSION, _MODERN_CIVIC_OCCUPATION_PACK_VERSION} or (
            pack_id == "modern_city" and pack_version in _PUBLIC_WAYPOINT_PACK_VERSIONS
        ):
            if pack_version not in _PUBLIC_WAYPOINT_PACK_VERSIONS and population_mode != "sustained":
                raise ValueError(f"Pack version {pack_version} is supported only with sustained population mode.")
            if pack_id != "modern_city":
                raise ValueError(f"Pack version {pack_version} is supported only for the modern_city background.")
            profiles = build_modern_city_occupation_profiles_260()
            validate_modern_city_occupation_contract_260(
                regions=pack.regions,
                organizations=pack.organizations,
                profiles=profiles,
                roles=pack.extra_roles,
                recruitment_roles=RECRUITMENT_ROLES[pack_id],
            )
            pack = replace(
                pack,
                version=pack_version,
                regional_occupation_profiles=profiles,
            )
        elif pack_version == _FEUDAL_OCCUPATION_PACK_VERSION_290:
            if population_mode != "sustained":
                raise ValueError("Pack version 2.9.0 is supported only with sustained population mode.")
            if pack_id != "low_magic_feudal":
                raise ValueError("Pack version 2.9.0 is supported only for the low_magic_feudal background.")
            profiles = build_feudal_occupation_profiles_290()
            validate_feudal_occupation_contract_290(
                regions=pack.regions,
                organizations=pack.organizations,
                profiles=profiles,
                roles=pack.extra_roles,
                recruitment_roles=RECRUITMENT_ROLES[pack_id],
                organization_home_regions=FEUDAL_ORGANIZATION_HOME_REGIONS_290,
            )
            pack = replace(
                pack,
                version=_FEUDAL_OCCUPATION_PACK_VERSION_290,
                regional_occupation_profiles=profiles,
            )
        elif pack_version == _CULTIVATION_OCCUPATION_PACK_VERSION_211:
            if population_mode != "sustained":
                raise ValueError("Pack version 2.11.0 is supported only with sustained population mode.")
            if pack_id != "long_life_cultivation":
                raise ValueError("Pack version 2.11.0 is supported only for the long_life_cultivation background.")
            profiles = build_cultivation_occupation_profiles_211()
            pack = replace(
                pack,
                version=_CULTIVATION_OCCUPATION_PACK_VERSION_211,
                regional_occupation_profiles=profiles,
            )
        elif pack_version in _PRICING_PACK_VERSIONS:
            pack = replace(pack, version=pack_version)
        elif pack_version == _INVESTIGATION_PACK_VERSION:
            pack = replace(pack, version=_INVESTIGATION_PACK_VERSION)
        elif population_mode == "sustained" and pack_version == _SUSTAINED_PACK_VERSION:
            pack = replace(pack, version=_SUSTAINED_PACK_VERSION)
        if pack_version is not None and pack_version != pack.version:
            raise ValueError("Unsupported version for this background and population mode.")
        if len(pack.regions) != _REGION_COUNT:
            raise ValueError(f"Pack {pack.pack_id!r} must define exactly {_REGION_COUNT} regions.")
        if len(pack.polities) != _POLITY_COUNT or len(pack.organizations) != _ORGANIZATION_COUNT:
            raise ValueError("The full world profile requires 24 polities and 24 other organizations.")
        if len(pack.key_names) != _KEY_PEOPLE or len(pack.key_people) != _KEY_PEOPLE:
            raise ValueError("The full world profile requires 32 named key people with authored profiles.")
        if len(pack.settlement_names) != _SETTLEMENTS_PER_REGION:
            raise ValueError("Each region must define nine settlement name patterns.")

        seed_text = f"{pack.pack_id}|{pack.version}|{world_title}|{player_display_name}"
        if seed is not None:
            seed_text += f"|seed:{seed}"
        if population_mode == "sustained":
            seed_text += "|population_mode:sustained"
        namespace = uuid5(_CONTENT_NAMESPACE, seed_text)
        state = _WorldSeedBuilder(
            namespace, pack, world_title, player_display_name,
            generation_seed=seed,
            population_mode=population_mode,
        ).build()
        if pack.version == _ORGANIZATION_STRATEGY_PACK_VERSION:
            content = state.metadata["content_index"]
            for identifier in (content["country_ids"][0], content["organization_ids"][0]):
                organization_id = UUID(identifier)
                components = state.entities[organization_id].components
                state = reserve_strategy(
                    state, organization_id, UUID(components["treasury"]["accounts"][pack.currency_id]),
                    mission=components["governance"]["charter"], scale=25,
                )
        return NewWorld(
            world_id=uuid5(namespace, "world"),
            branch_id=uuid5(namespace, "branch:main"),
            title=world_title,
            ruleset_version=_RULESET_VERSION,
            pack_version=pack.version,
            state=state,
        )


class _WorldSeedBuilder:
    def __init__(
        self,
        namespace: UUID,
        pack: BackgroundDefinition,
        title: str,
        player_name: str,
        *,
        generation_seed: int | None = None,
        population_mode: str = "natural",
    ) -> None:
        self.namespace = namespace
        self.pack = pack
        self.title = title
        self.player_name = player_name
        self.generation_seed = generation_seed
        self.population_mode = population_mode
        self.resident_count = _named_resident_capacity(pack.version, population_mode)
        self.routine_opening = ROUTINE_OPENINGS[pack.pack_id] if pack.version == ROUTINE_OPENING_PACK_VERSION else None
        self.uses_modern_occupation_profiles = pack.pack_id == "modern_city" and pack.version in {
            *_MODERN_OCCUPATION_PROFILE_VERSIONS, *_PUBLIC_WAYPOINT_PACK_VERSIONS,
            _MODERN_CIVIC_RECRUITMENT_PACK_VERSION_210,
        }
        self.uses_feudal_occupation_profiles = (
            pack.pack_id == "low_magic_feudal" and pack.version == _FEUDAL_OCCUPATION_PACK_VERSION_290
        )
        self.uses_cultivation_population_profiles = (
            pack.pack_id == "long_life_cultivation"
            and pack.version == _CULTIVATION_OCCUPATION_PACK_VERSION_211
        )
        self.entities: dict[str, dict[str, Any]] = {}
        self.resources: dict[str, dict[str, Any]] = {}
        self.relations: list[dict[str, Any]] = []
        self.knowledge: list[dict[str, Any]] = []
        self.goals: list[dict[str, Any]] = []
        self.accounts_by_owner_resource: dict[tuple[UUID, str], UUID] = {}
        self.holdings: dict[UUID, list[dict[str, Any]]] = {}
        self.endowment_index: list[dict[str, Any]] = []
        self.source_account_index: dict[str, dict[str, Any]] = {}
        self.sink_account_ids: dict[str, UUID] = {}
        self.issuance_source_account_ids: dict[str, UUID] = {}
        self.harvest_policy_index: list[dict[str, Any]] = []
        self.scheduled_events: list[dict[str, Any]] = []

        self.region_ids = [self.entity_id("region", index) for index in range(_REGION_COUNT)]
        self.settlement_ids = [
            self.entity_id("settlement", region_index, settlement_index)
            for region_index in range(_REGION_COUNT)
            for settlement_index in range(_SETTLEMENTS_PER_REGION)
        ]
        # Reuse one of the declared 120 place slots for each pack's hidden
        # historical site, keeping the public place capacity exact.
        self.discovery_site_id = self.settlement_ids[_SETTLEMENTS_PER_REGION - 1]
        self.polity_ids = [self.entity_id("polity", index) for index in range(_POLITY_COUNT)]
        self.organization_ids = [self.entity_id("organization", index) for index in range(_ORGANIZATION_COUNT)]
        self.player_id = self.entity_id("character", "player")
        self.key_ids = [self.entity_id("character", "key", index) for index in range(_KEY_PEOPLE)]
        self.resident_ids = [
            self.entity_id("character", "resident", index) for index in range(self.resident_count)
        ]
        self.group_ids = [self.entity_id("population", index) for index in range(_REGION_COUNT)]

    def entity_id(self, *parts: object) -> UUID:
        return uuid5(self.namespace, ":".join(str(part) for part in parts))

    def build(self) -> WorldState:
        self._build_places()
        if self.pack.version in {
            *_PUBLIC_WAYPOINT_PACK_VERSIONS, _FEUDAL_OCCUPATION_PACK_VERSION_290,
            _MODERN_CIVIC_RECRUITMENT_PACK_VERSION_210,
            _CULTIVATION_OCCUPATION_PACK_VERSION_211,
        }:
            for place_id, offers in build_waypoint_openings(
                self.pack.pack_id, self.region_ids, self.settlement_ids, day_seconds=_DAY_SECONDS,
            ).items():
                place = self.entities[str(place_id)]["components"]["place"]
                place["waypoint_opportunities"] = [offer.model_dump(mode="json") for offer in offers]
                for offer in offers:
                    self._put_entity(
                        uuid5(offer.opportunity_id, "notice"), "item", f"告示｜{offer.title}",
                        location_id=place_id, components={"public_description": waypoint_opening_notice(offer)},
                    )
        self._seed_system_resource_accounts()
        self._build_characters()
        if self.routine_opening is not None:
            self._build_routine_opening(self.routine_opening)
        self._build_organizations()
        self._build_population_groups()
        self._build_population_schedule()
        self._build_harvest_policies()
        self._build_markets_and_production()
        if self.pack.version in _PRICING_PACK_VERSIONS:
            self._build_market_review_schedule()
        self._build_relationships()
        self._build_goals_and_knowledge()
        if self.pack.version in _CASE_PACK_VERSIONS or self.pack.version == _INVESTIGATION_PACK_VERSION:
            self._build_opening_investigation()
        self._balance_genesis_sources()
        work_postings = self._build_work_postings() if self.pack.version in _FAMILY_PACK_VERSIONS else []
        metadata = self._metadata()
        if work_postings:
            metadata["work_shift_terms"] = [
                {
                    "posting_id": posting["posting_id"],
                    "duration_seconds": _HOUR_SECONDS,
                    "resource_id": self.pack.currency_id,
                    "wage": "3",
                }
                for posting in work_postings
            ]

        state = WorldState.model_validate(
            {
                "current_tick": 0,
                "entities": self.entities,
                "resources": self.resources,
                "relations": self.relations,
                "knowledge": self.knowledge,
                "goals": self.goals,
                "scheduled_events": self.scheduled_events,
                "kinship_links": self._build_family_links() if self.pack.version in _FAMILY_PACK_VERSIONS else [],
                "work_postings": work_postings,
                "player_id": self.player_id,
                "calendar": {
                    "day_seconds": _DAY_SECONDS,
                    "year_days": self.pack.year_days,
                    "calendar_id": self.pack.calendar_name,
                },
                "metadata": metadata,
            }
        )
        self._validate_built_capacity(state)
        return state

    def _metadata(self) -> dict[str, Any]:
        metadata = {
            "pack_id": self.pack.pack_id,
            "pack_version": self.pack.version,
            "population_mode": self.population_mode,
            "time_units": {
                "tick": "second",
                "day_seconds": _DAY_SECONDS,
                "year_days": self.pack.year_days,
                "route_durations": "hours for modern_city; days for other packs except one-hour hidden-site links",
                "production_durations": "hours for modern_city; days for other packs",
            },
            "public_title": self.title,
            "public_description": self.pack.summary,
            "content_rules": self.pack.rules,
            "systems": list(self.pack.systems),
            "themes": list(self.pack.themes),
            "scenario_hooks": list(self.pack.scenario_hooks),
            "resource_catalog": {resource_id: resource_id.rsplit(".", 1)[-1].replace("_", " ") for resource_id in self.pack.resources},
            "resource_roles": {
                "food_resource_id": self._food_resource(),
                "water_resource_id": self._water_resource(),
                "consumption_sink_account_ids": {key: str(value) for key, value in self.sink_account_ids.items()},
                "production_source_accounts": self.source_account_index,
                "issuance_source_account_ids": {key: str(value) for key, value in self.issuance_source_account_ids.items()},
                "environmental_harvest_policies": self.harvest_policy_index,
                "environmental_endowments": self.endowment_index,
            },
            "content_index": {
                "region_ids": [str(value) for value in self.region_ids],
                "settlement_ids": [
                    str(value) for value in self.settlement_ids if value != self.discovery_site_id
                ],
                "country_ids": [str(value) for value in self.polity_ids],
                "organization_ids": [str(value) for value in self.organization_ids],
                "key_npc_ids": [str(value) for value in self.key_ids],
                "other_resident_ids": [str(value) for value in self.resident_ids],
                "player_id": str(self.player_id),
                "population_group_ids": [str(value) for value in self.group_ids],
            },
            "seed_counts": {
                "countries": len(self.polity_ids),
                "organizations": len(self.organization_ids),
                "key_npcs": len(self.key_ids),
                "other_named_living_characters": len(self.resident_ids),
                "places": len(self.region_ids) + len(self.settlement_ids),
            },
            "profile": "full",
        }
        if self.generation_seed is not None:
            metadata["generation_seed"] = self.generation_seed
        if self.pack.pack_id == "modern_city" and self.pack.version == _ORGANIZATION_STRATEGY_PACK_VERSION:
            investigation = {
                "goal_id": str(self.entity_id("goal", "player")),
                "destination_id": str(self.settlement_ids[1]),
            }
            if self.pack.version in _CASE_PACK_VERSIONS:
                investigation["anchor_id"] = str(self.entity_id("item", "opening-investigation-case"))
                investigation["case_id"] = str(self.entity_id("case", "opening-investigation-rumor"))
            metadata["opening_investigation"] = investigation
        if self.routine_opening is not None:
            metadata["time_units"]["route_durations"] += "; short local routine links are measured in minutes"
        if self._has_membership_recruitment():
            metadata["content_index"]["other_resident_population_groups"] = {
                str(resident_id): self.entities[str(resident_id)]["components"]["life"]["population_group_id"]
                for resident_id in self.resident_ids
            }
        if self.pack.version in _PRICING_PACK_VERSIONS:
            metadata[PRICE_REVIEW_CONFIG_KEY] = self._market_pricing_config()
        if self.pack.version in {_TRUST_PACK_VERSION, *_FAMILY_PACK_VERSIONS}:
            metadata[PROMISE_TRUST_POLICY_KEY] = PROMISE_TRUST_POLICY_ID
        return metadata

    def _seeded_int(self, key: str, minimum: int, maximum: int) -> int:
        if self.generation_seed is None:
            raise RuntimeError("seeded variation requires an explicit generation seed")
        if maximum < minimum:
            raise ValueError("seeded integer range is inverted")
        material = f"{self.generation_seed}|{self.pack.pack_id}|{key}".encode("utf-8")
        value = int.from_bytes(hashlib.sha256(material).digest()[:8], "big")
        return minimum + value % (maximum - minimum + 1)

    def _seeded_order(self, values: list[Any], key: str) -> list[Any]:
        if self.generation_seed is None:
            return list(values)
        indexed = enumerate(values)
        return [
            item for _index, item in sorted(
                indexed,
                key=lambda pair: hashlib.sha256(
                    f"{self.generation_seed}|{self.pack.pack_id}|{key}|{pair[0]}".encode("utf-8")
                ).digest(),
            )
        ]

    def _put_entity(
        self,
        entity_id: UUID,
        kind: str,
        name: str,
        *,
        location_id: UUID | None = None,
        components: dict[str, Any] | None = None,
    ) -> None:
        self.entities[str(entity_id)] = {
            "id": str(entity_id),
            "kind": kind,
            "name": name,
            "location_id": str(location_id) if location_id else None,
            "status": "active",
            "components": components or {},
        }

    def _seed_system_resource_accounts(self) -> None:
        endowment_by_resource: dict[str, Decimal] = {}
        for endowment in self.endowment_index:
            resource_id = endowment["resource_id"]
            endowment_by_resource[resource_id] = (
                endowment_by_resource.get(resource_id, Decimal("0")) + Decimal(endowment["quantity"])
            )
        for resource_id in self.pack.resources:
            sink_id = self.entity_id("system-account", "consumption-sink", resource_id)
            finite_quota = endowment_by_resource.get(resource_id, Decimal("0"))
            self.sink_account_ids[resource_id] = sink_id
            self.source_account_index[resource_id] = {
                "aggregate_credit_limit": "0",
                "producer_accounts": [],
                "authorization": "per-producer quotas are bounded by finite seeded recipe inputs",
            }
            self.resources[str(sink_id)] = {
                "resource_id": resource_id,
                "balance": "0",
                "owner_id": None,
                "account_type": "consumption_sink",
            }

    def _balance_genesis_sources(self) -> None:
        """Balance authored starting holdings with explicit bounded issuance sources."""
        for resource_id in self.pack.resources:
            issued = sum(
                (
                    Decimal(account["balance"])
                    for account in self.resources.values()
                    if account["resource_id"] == resource_id
                    and account["account_type"] == "holder"
                    and account["owner_id"] is not None
                ),
                Decimal("0"),
            )
            account_id = self.entity_id("system-account", "genesis-issuance", resource_id)
            self.issuance_source_account_ids[resource_id] = account_id
            self.resources[str(account_id)] = {
                "resource_id": resource_id,
                "balance": _decimal_string(-issued),
                "owner_id": None,
                "account_type": "issuance_source",
                "credit_limit": _decimal_string(issued),
            }

    def _seed_region_transfer(
        self, region_id: UUID, holder_id: UUID, resource_id: str, quantity: Decimal
    ) -> dict[str, Any]:
        """Transfer a finite genesis stock allocation from a region to a holder."""
        source_account_id = self.accounts_by_owner_resource.get((region_id, resource_id))
        if source_account_id is None:
            raise ValueError(f"Region {region_id} has no finite {resource_id} stock to allocate.")
        source_account = self.resources[str(source_account_id)]
        source_balance = Decimal(source_account["balance"])
        if quantity <= 0 or source_balance < quantity:
            raise ValueError(f"Region {region_id} has insufficient {resource_id} stock for genesis allocation.")
        source_account["balance"] = _decimal_string(source_balance - quantity)
        source_lot = next(
            (lot for lot in self.holdings.get(region_id, []) if lot["resource_id"] == resource_id),
            None,
        )
        if source_lot is None or Decimal(source_lot["quantity"]) < quantity:
            raise ValueError(f"Region {region_id} has no matching inventory lot for {resource_id}.")
        source_lot["quantity"] = _decimal_string(Decimal(source_lot["quantity"]) - quantity)
        endowment = next(
            item for item in self.endowment_index
            if item["region_id"] == str(region_id) and item["resource_id"] == resource_id
        )
        endowment["remaining_quantity"] = _decimal_string(Decimal(endowment["remaining_quantity"]) - quantity)
        endowment["allocations"].append({"holder_id": str(holder_id), "quantity": _decimal_string(quantity)})
        return self._grant_holding(holder_id, resource_id, _decimal_string(quantity))

    def _food_resource(self) -> str:
        for candidate in ("food", "grain"):
            resource_id = next(
                (value for value in self.pack.resources if value.rsplit(".", 1)[-1] == candidate),
                None,
            )
            if resource_id is not None:
                return resource_id
        raise ValueError(f"Pack {self.pack.pack_id!r} must define a food or grain resource.")

    def _water_resource(self) -> str:
        for candidate in ("water", "fresh_water", "spirit_water"):
            resource_id = next(
                (value for value in self.pack.resources if value.rsplit(".", 1)[-1] == candidate),
                None,
            )
            if resource_id is not None:
                return resource_id
        raise ValueError(f"Pack {self.pack.pack_id!r} must define a water resource.")

    def _secondary_resource(self, primary_resource: str, ordinal: int) -> str:
        excluded = {primary_resource, self.pack.currency_id, self._food_resource(), self._water_resource()}
        candidates = [resource_id for resource_id in self.pack.resources if resource_id not in excluded]
        if not candidates:
            raise ValueError(f"Pack {self.pack.pack_id!r} needs a secondary physical resource.")
        return candidates[ordinal % len(candidates)]

    @staticmethod
    def _endowment_amount(resource_id: str) -> int:
        suffix = resource_id.rsplit(".", 1)[-1]
        if suffix in {"food", "grain"}:
            return 20_000
        if suffix in {"water", "fresh_water", "spirit_water"}:
            return 20_000
        if suffix in {"timber", "wool", "herbs", "medicine"}:
            return 8_000 if suffix in {"timber", "wool"} else 3_000
        if suffix in {"iron", "ore", "materials"}:
            return 12_000
        if suffix in {"salt", "fuel"}:
            return 5_000
        if suffix in {"transit_capacity", "data_bandwidth"}:
            return 50_000
        if suffix in {"ink", "qi", "spirit_stone"}:
            return 3_000
        return 1_000

    def _build_places(self) -> None:
        for index, region in enumerate(self.pack.regions):
            region_id = self.region_ids[index]
            region_settlements = self.settlement_ids[
                index * _SETTLEMENTS_PER_REGION : (index + 1) * _SETTLEMENTS_PER_REGION
            ]
            regional_unit = "hour" if self.pack.pack_id == "modern_city" else "day"
            routes = [
                self._route(
                    region_id,
                    place_id,
                    duration=(
                        1
                        if place_id == self.discovery_site_id
                        else 2 + settlement_index % 3 if regional_unit == "hour" else 1 + settlement_index % 3
                    ),
                    unit="hour" if place_id == self.discovery_site_id else regional_unit,
                    route_key=f"region:{index}:out:{settlement_index}",
                )
                for settlement_index, place_id in enumerate(region_settlements)
            ]
            routes.append(
                self._route(
                    region_id,
                    self.region_ids[(index + 1) % len(self.region_ids)],
                    duration=4 + index % 5 if regional_unit == "hour" else 2 + index % 4,
                    unit=regional_unit,
                    route_key=f"region:{index}:next_region",
                )
            )
            primary_resource = self._resource_for_output(region.main_output)
            secondary_resource = self._secondary_resource(primary_resource, index)
            endowment_resources = list(dict.fromkeys(
                [primary_resource, secondary_resource, self._food_resource(), self._water_resource()]
                + (["cultivation.qi"] if self.pack.pack_id == "long_life_cultivation" else [])
            ))
            endowment_lots = [
                self._grant_holding(region_id, resource_id, str(self._endowment_amount(resource_id)))
                for resource_id in endowment_resources
            ]
            self.endowment_index.extend(
                {
                    "region_id": str(region_id),
                    "account_id": lot["account_id"],
                    "resource_id": lot["resource_id"],
                    "quantity": lot["quantity"],
                    "remaining_quantity": lot["quantity"],
                    "allocations": [],
                }
                for lot in endowment_lots
            )
            self._put_entity(
                region_id,
                "place",
                region.name,
                components={
                    "place": {
                        "place_type": "region",
                        "capacity": 250_000,
                        "access_policy": "open",
                        "terrain": region.terrain,
                        "environment": {
                            "environment.primary_output": region.main_output,
                            "environment.hazard": region.hazard,
                            "environment.setting": self.pack.pack_id,
                        },
                        "routes": routes,
                    },
                    "inventory": {"lots": endowment_lots},
                    "public_description": f"{region.description} Regional pressure: {region.hazard}.",
                },
            )

            for settlement_index, settlement_id in enumerate(region_settlements):
                pattern = self.pack.settlement_names[settlement_index]
                settlement_kind = self.pack.settlement_kinds[settlement_index]
                is_discovery_site = settlement_id == self.discovery_site_id
                settlement_name = (
                    self.pack.discovery_site.name
                    if is_discovery_site
                    else f"{pattern} of {region.name}"
                )
                next_id = region_settlements[(settlement_index + 1) % len(region_settlements)]
                settlement_unit = "hour" if self.pack.pack_id == "modern_city" else "day"
                if is_discovery_site:
                    # The site is reached by a short walk from both the
                    # player's opening settlement and its parent region.
                    # Its inbound routes remain unusable until discovery.
                    routes = [
                        self._route(
                            settlement_id, self.settlement_ids[0], duration=1, unit="hour",
                            route_key=f"discovery-site:{self.pack.pack_id}:to-start",
                        ),
                        self._route(
                            settlement_id, region_id, duration=1, unit="hour",
                            route_key=f"discovery-site:{self.pack.pack_id}:to-region",
                        ),
                    ]
                else:
                    routes = [
                        self._route(
                            settlement_id, region_id,
                            duration=1,
                            unit=settlement_unit,
                            route_key=f"settlement:{index}:{settlement_index}:parent",
                        ),
                        self._route(
                            settlement_id, next_id,
                            duration=1 if next_id == self.discovery_site_id else 1 + (settlement_index % 4),
                            unit="hour" if next_id == self.discovery_site_id else settlement_unit,
                            route_key=f"settlement:{index}:{settlement_index}:next",
                        ),
                    ]
                    if settlement_id == self.settlement_ids[0]:
                        routes.append(
                            self._route(
                                settlement_id, self.discovery_site_id, duration=1, unit="hour",
                                route_key=f"discovery-site:{self.pack.pack_id}:from-start",
                            )
                        )
                resource_id = self._resource_for_output(region.main_output)
                self._put_entity(
                    settlement_id,
                    "place",
                    settlement_name,
                    location_id=self.settlement_ids[0] if is_discovery_site else region_id,
                    components={
                        "place": {
                            "place_type": "site" if is_discovery_site else "settlement",
                            "region_id": str(region_id),
                            "parent_id": str(self.settlement_ids[0] if is_discovery_site else region_id),
                            "capacity": 2_000 + (settlement_index * 750),
                            "access_policy": "open",
                            "terrain": region.terrain,
                            "environment": {
                                "environment.kind": "historical_site" if is_discovery_site else settlement_kind,
                                "environment.primary_output": resource_id,
                            },
                            "routes": routes,
                        },
                        **({"hidden": True} if is_discovery_site else {}),
                        "public_description": (
                            self.pack.discovery_site.description
                            if is_discovery_site
                            else (
                                f"{settlement_kind.replace('_', ' ').capitalize()} in {region.name}. "
                                f"Its main local output is {region.main_output.replace('_', ' ')}."
                            )
                        ),
                    },
                )

    def _route(
        self,
        origin: UUID,
        destination: UUID,
        *,
        duration: int,
        unit: str,
        route_key: str,
    ) -> dict[str, Any]:
        if unit not in {"hour", "day"} or duration <= 0:
            raise ValueError("Route durations must use a positive number of hours or days.")
        travel_ticks = duration * (_HOUR_SECONDS if unit == "hour" else _DAY_SECONDS)
        return {
            "route_id": str(self.entity_id("route", route_key)),
            "from_place_id": str(origin),
            "to_place_id": str(destination),
            "travel_ticks": travel_ticks,
            "capacity": 500,
            "risk": "0.05" if travel_ticks <= 12 * _HOUR_SECONDS else "0.12",
            "access_tags": [],
            "travel_modes": ["travel.walk"],
            "is_open": True,
        }

    def _build_characters(self) -> None:
        characters_at: dict[UUID, list[UUID]] = {place_id: [] for place_id in self.settlement_ids}
        start_place_id = self.settlement_ids[0]
        characters_at[start_place_id].append(self.player_id)
        self._add_character(
            self.player_id,
            self.player_name,
            start_place_id,
            region_index=0,
            profile=None,
            role="new arrival",
            goal_title=f"Understand the local situation",
            goal_description=self.pack.scenario_hooks[0],
            tier="player",
            age_years=24,
            generation_slot="player",
        )

        self.key_names: list[str] = []
        self.key_locations: list[UUID] = []
        key_profiles = list(zip(self.pack.key_names, self.pack.key_people, strict=True))
        key_profiles = self._seeded_order(key_profiles, "key-profile-pairs")
        self.key_profiles: list[PersonSeed] = []
        for index, ((given, family), profile) in enumerate(key_profiles):
            name = f"{given} {family}"
            place_index = (index * 3 + 2) % len(self.settlement_ids)
            if self.generation_seed is not None:
                region_index = place_index // _SETTLEMENTS_PER_REGION
                local_index = self._seeded_int(f"key-place:{index}", 0, _SETTLEMENTS_PER_REGION - 1)
                place_index = region_index * _SETTLEMENTS_PER_REGION + local_index
            if self.routine_opening is not None and profile.role == self.routine_opening.role:
                place_index = 1
            home = self.settlement_ids[place_index]
            characters_at[home].append(self.key_ids[index])
            self.key_names.append(name)
            self.key_locations.append(home)
            self.key_profiles.append(profile)
            self._add_character(
                self.key_ids[index],
                name,
                home,
                region_index=place_index // _SETTLEMENTS_PER_REGION,
                profile=profile,
                role=profile.role,
                goal_title=profile.goal_title,
                goal_description=profile.goal,
                tier="key_npc",
                age_years=22 + ((index * 7) % 45),
                generation_slot=f"key:{index}",
            )

        resident_names = self._resident_names()
        resident_roles = [
            self.pack.extra_roles[index % len(self.pack.extra_roles)]
            for index in range(self.resident_count)
        ]
        resident_roles = self._seeded_order(resident_roles, "resident-role-assignment")
        resident_place_indexes = [index % len(self.settlement_ids) for index in range(_OTHER_RESIDENTS)]
        if self.resident_count > _OTHER_RESIDENTS:
            for region_index in range(_REGION_COUNT):
                local_indexes = [
                    local_index for local_index in range(_SETTLEMENTS_PER_REGION)
                    if region_index != 0 or local_index != _SETTLEMENTS_PER_REGION - 1
                ]
                extra_settlement_order = self._seeded_order(
                    local_indexes,
                    f"resident-extra-settlement-order:{region_index}",
                )
                for offset in range(2):
                    resident_place_indexes.append(
                        region_index * _SETTLEMENTS_PER_REGION + extra_settlement_order[offset]
                    )
        if self.generation_seed is not None:
            for region_index in range(_REGION_COUNT):
                regional_residents = [
                    index for index in range(_OTHER_RESIDENTS)
                    if resident_place_indexes[index] // _SETTLEMENTS_PER_REGION == region_index
                ]
                local_order = self._seeded_order(
                    list(range(_SETTLEMENTS_PER_REGION)),
                    f"resident-settlement-order:{region_index}",
                )
                for ordinal, resident_index in enumerate(regional_residents):
                    resident_place_indexes[resident_index] = (
                        region_index * _SETTLEMENTS_PER_REGION
                        + local_order[ordinal % _SETTLEMENTS_PER_REGION]
                    )
        if self.uses_modern_occupation_profiles or self.uses_feudal_occupation_profiles:
            resident_roles = self._regional_resident_roles(resident_place_indexes)
        self.resident_names: list[str] = []
        self.resident_roles: list[str] = []
        for index, (resident_id, name, role) in enumerate(
            zip(self.resident_ids, resident_names, resident_roles, strict=True)
        ):
            place_index = resident_place_indexes[index]
            home = self.settlement_ids[place_index]
            region_index = place_index // _SETTLEMENTS_PER_REGION
            characters_at[home].append(resident_id)
            self.resident_names.append(name)
            self.resident_roles.append(role)
            self._add_character(
                resident_id,
                name,
                home,
                region_index=region_index,
                profile=None,
                role=role,
                goal_title=f"Build a dependable {self.pack.regions[region_index].main_output.replace('_', ' ')} reserve",
                goal_description=(
                    f"Keep enough {self.pack.regions[region_index].main_output.replace('_', ' ')} available "
                    f"to meet the needs of a {role}."
                ),
                tier="resident",
                age_years=18 + (index % 48),
                generation_slot=f"resident:{index}",
                role_skill_levels=self._resident_role_skill_levels(region_index, role),
            )

        self.characters_at = characters_at

    def _build_routine_opening(self, opening: RoutineOpening) -> None:
        (actor_id,) = (key_id for key_id, profile in zip(self.key_ids, self.key_profiles, strict=True)
                       if profile.role == opening.role)
        primary_id, home_id, fallback_id = self.settlement_ids[:3]
        notice = prepare_routine_opening(
            opening, self.entities[str(actor_id)], self.entities[str(primary_id)],
            self.entities[str(home_id)], self.entities[str(fallback_id)],
        )
        self.entities[str(self.player_id)]["components"]["known_places"].append(str(fallback_id))
        self._put_entity(
            self.entity_id("item", "routine-opening-notice"), "item", "告示｜例行会面时间",
            location_id=primary_id, components={"public_description": notice},
        )


    def _build_family_links(self) -> list[dict[str, Any]]:
        year_ticks = self.pack.year_days * _DAY_SECONDS
        links = []
        for region_index in range(_REGION_COUNT):
            places = self.settlement_ids[
                region_index * _SETTLEMENTS_PER_REGION:(region_index + 1) * _SETTLEMENTS_PER_REGION
            ]
            people = sorted(
                (person for place in places for person in self.characters_at[place] if person != self.player_id),
                key=lambda person: (self.entities[str(person)]["components"]["life"]["birth_tick"], str(person)),
            )
            parent, child = people[0], people[-1]
            parent_birth = self.entities[str(parent)]["components"]["life"]["birth_tick"]
            child_birth = self.entities[str(child)]["components"]["life"]["birth_tick"]
            if child_birth - parent_birth < 18 * year_ticks:
                continue
            adoptive = region_index % 3 == 2
            links.append({
                "link_id": str(self.entity_id("kinship", parent, child)),
                "parent_id": str(parent), "child_id": str(child),
                "relationship": "adoptive" if adoptive else "birth",
                "created_tick": child_birth + 6 * year_ticks if adoptive else child_birth,
                "source": {"kind": "genesis", "reference_id": f"{self.pack.pack_id}:{self.pack.version}:family:{region_index}"},
            })
        return links

    def _build_work_postings(self) -> list[dict[str, Any]]:
        title = {
            "low_magic_feudal": "粮仓登记员",
            "modern_city": "仓储调度助理",
            "long_life_cultivation": "灵材库协理",
        }[self.pack.pack_id]
        return [
            {
                "posting_id": str(self.entity_id("work-posting", region_index)),
                "employer_id": str(self.organization_ids[self._work_posting_employer_index(region_index)]),
                "location_id": str(self.settlement_ids[region_index * _SETTLEMENTS_PER_REGION]),
                "title": title,
                "required_skill": {"skill_id": "skill.local_knowledge", "minimum_level": "10"},
                "slots": 2,
                "valid_from_tick": 0,
            }
            for region_index in range(_REGION_COUNT)
        ]

    def _work_posting_employer_index(self, region_index: int) -> int:
        if not self.uses_feudal_occupation_profiles:
            return region_index * 2
        region_name = self.pack.regions[region_index].name
        return next(
            index for index, organization in enumerate(self.pack.organizations)
            if FEUDAL_ORGANIZATION_HOME_REGIONS_290[organization.name] == region_name
        )

    def _resident_names(self) -> list[str]:
        excluded = {name.casefold() for name in self.key_names}
        excluded.add(self.player_name.casefold())
        names: list[str] = []
        for given, family in product(self.pack.extra_given_names, self.pack.extra_family_names):
            full_name = f"{given} {family}"
            if full_name.casefold() in excluded:
                continue
            names.append(full_name)
            if len(names) == self.resident_count:
                return self._seeded_order(names, "resident-name-assignment")
        raise ValueError(f"Pack name pools do not provide {self.resident_count} unique resident names.")

    def _regional_occupation_profile(self, region_index: int):
        if not (self.uses_modern_occupation_profiles or self.uses_feudal_occupation_profiles):
            return None
        profiles = self.pack.regional_occupation_profiles or {}
        return profiles[self.pack.regions[region_index].name]

    def _regional_population_seat_profile(self, region_index: int):
        if self.uses_cultivation_population_profiles:
            profiles = self.pack.regional_occupation_profiles or {}
            return profiles[self.pack.regions[region_index].name]
        return self._regional_occupation_profile(region_index)

    def _resident_role_skill_levels(self, region_index: int, role: str) -> dict[str, str] | None:
        profile = self._regional_occupation_profile(region_index)
        if profile is None:
            return None
        return profile.role_skill_levels.get(role, {})

    def _regional_resident_roles(self, resident_place_indexes: list[int]) -> list[str]:
        regional_serials: dict[int, int] = {}
        roles: list[str] = []
        for place_index in resident_place_indexes:
            region_index = place_index // _SETTLEMENTS_PER_REGION
            profile = self._regional_occupation_profile(region_index)
            if profile is None:
                raise ValueError("Regional occupation pack is missing a regional occupation profile.")
            serial = regional_serials.get(region_index, 0)
            regional_serials[region_index] = serial + 1
            available_roles = self.pack.extra_roles
            if self.uses_feudal_occupation_profiles:
                available_roles = [role for role in available_roles if role in profile.role_weights]
            roles.append(
                select_population_occupation_role(
                    available_roles,
                    profile.role_weights,
                    seed_id=self._population_seat_seed_id(region_index),
                    group_id=self.group_ids[region_index],
                    period_index=0,
                    serial=serial,
                )
            )
        return roles

    def _population_seat_seed_id(self, region_index: int) -> UUID:
        seat_seed_basis = (
            f"{self.pack.pack_id}|{self.pack.version}|explicit:{self.generation_seed}"
            if self.generation_seed is not None
            else f"{self.pack.pack_id}|{self.pack.version}|{self.title}|{self.player_name}"
        )
        seat_seed_namespace = uuid5(_CONTENT_NAMESPACE, f"population-seats:{seat_seed_basis}")
        return uuid5(seat_seed_namespace, f"region:{region_index}")

    def _add_character(
        self,
        entity_id: UUID,
        name: str,
        home: UUID,
        *,
        region_index: int,
        profile: PersonSeed | None,
        role: str,
        goal_title: str,
        goal_description: str,
        tier: str,
        age_years: int,
        generation_slot: str,
        role_skill_levels: dict[str, str] | None = None,
    ) -> None:
        if self.generation_seed is not None:
            age_years = max(
                18,
                age_years + self._seeded_int(f"age:{generation_slot}", -4, 4),
            )
        region = self.pack.regions[region_index]
        home_name = self.entities[str(home)]["name"]
        primary_resource = self._resource_for_output(region.main_output)
        is_cultivator = self.pack.pack_id == "long_life_cultivation" and any(
            marker in role.casefold() for marker in ("cultivat", "disciple", "master", "sect")
        )
        lifespan_years = 1_200 if is_cultivator else (100 if self.pack.pack_id == "long_life_cultivation" else 82)
        year_ticks = self.pack.year_days * _DAY_SECONDS
        lifespan_ticks = lifespan_years * year_ticks
        granted_resources: dict[str, Decimal] = {}
        for resource_id, quantity in (
            (self.pack.currency_id, 35 if tier == "player" else (15 + (len(name) % 43))),
            (primary_resource, 5 if tier == "player" else (3 + (len(name) % 11))),
            (self._food_resource(), 54),
            (self._water_resource(), 90),
        ):
            granted_resources[resource_id] = granted_resources.get(resource_id, Decimal("0")) + Decimal(quantity)
        if is_cultivator:
            qi_amount = Decimal(1 + (sum(ord(char) for char in name) % 8))
            granted_resources["cultivation.qi"] = granted_resources.get("cultivation.qi", Decimal("0")) + qi_amount
        inventory_lots: list[dict[str, Any]] = []
        for resource_id, quantity in granted_resources.items():
            if resource_id == self.pack.currency_id:
                lot = self._grant_holding(entity_id, resource_id, _decimal_string(quantity))
            else:
                lot = self._seed_region_transfer(self.region_ids[region_index], entity_id, resource_id, quantity)
            inventory_lots.append(lot)

        skills = self._skills_for_role(role, profile, tier)
        for skill_id, level in (role_skill_levels or {}).items():
            skill = skills.setdefault(skill_id, {"experience": 0, "trained_ticks": 0})
            skill["level"] = str(level)
        values = list(profile.values) if profile else _words_from_role(role)
        traits = list(profile.traits) if profile else _traits_from_role(role)
        personality = _personality_for(name, traits, values)
        home_region = self.region_ids[region_index]
        known_places = [str(home), str(home_region)]
        if tier == "player":
            known_places.append(str(self.settlement_ids[1]))
        base_needs = {
            "food": 54 + (len(name) % 24),
            "water": 62 + (len(role) % 20),
            "rest": 48 + (len(name) % 29),
            "shelter": 70 + (len(role) % 20),
            "safety": 58 + (len(name) % 30),
            "health": 75 + (len(role) % 20),
            "social": 50 + (len(name) % 30),
            "belonging": 52 + (len(role) % 28),
        }
        if self.generation_seed is not None:
            base_needs = {
                need: max(
                    35,
                    min(95, value + self._seeded_int(f"needs:{generation_slot}:{need}", -7, 7)),
                )
                for need, value in base_needs.items()
            }
        components = {
            "life": {
                "species_id": "core.human",
                "birth_tick": -age_years * year_ticks,
                "lifespan_ticks": lifespan_ticks,
                "maturity_ticks": 18 * year_ticks,
                "population_group_id": str(self.group_ids[region_index]),
                "is_immortal": False,
                "life_stage": "elder" if age_years >= 65 else "adult",
            },
            "health": {
                "current": 100 - (len(role) % 18),
                "maximum": 100,
                "conditions": [],
                "injuries": [],
            },
            "needs": {
                need: {"value": value, "desired": 80, "critical_below": 20}
                for need, value in base_needs.items()
            },
            "skills": {"skills": skills},
            "personality": personality,
            "attributes": {
                "values": {
                    "attribute.reliability": str(35 + (len(name) % 61)),
                    "attribute.practical_judgment": str(40 + (len(role) % 55)),
                    "attribute.social_influence": str(25 + (len(values) * 9)),
                    "attribute.physical_endurance": str(30 + (age_years % 50)),
                }
            },
            "capabilities": {"grants": _capabilities_for(self.pack.pack_id, role, is_cultivator), "restrictions": []},
            "progression": {
                "experience": 0 if tier == "player" else 10 + len(role),
                "level": 2 + (sum(ord(char) for char in name) % 8) if is_cultivator else 1,
                "milestones": [],
                "next_threshold": 100,
            },
            "known_places": known_places,
            "inventory": {"lots": inventory_lots},
            "hidden": False,
            "public_description": (
                f"{name} works as a {role} in {home_name}. "
                f"{('Their current concern is ' + profile.goal.lower()) if profile else 'Their daily work is tied to local services and supplies.'}"
            ),
            "appearance": _appearance_for(name),
            "public_role": role,
        }
        self._put_entity(entity_id, "character", name, location_id=home, components=components)

    def _skills_for_role(self, role: str, profile: PersonSeed | None, tier: str) -> dict[str, dict[str, Any]]:
        role_tokens = set(role.casefold().replace("'", " ").split())
        result: dict[str, dict[str, Any]] = {}
        skills = ["skill.social", "skill.local_knowledge"]
        if role_tokens & {"farmer", "grower", "gardener", "herb", "food", "granary", "mill", "shepherd"}:
            skills.append("skill.agriculture")
        if role_tokens & {"mechanic", "repair", "smith", "maker", "craft", "engineer", "tool"}:
            skills.append("skill.repair")
        if role_tokens & {"scribe", "reporter", "journalist", "archivist", "analyst", "records", "researcher"}:
            skills.append("skill.research")
        if role_tokens & {"courier", "driver", "captain", "carter", "transit", "freight", "ferry"}:
            skills.append("skill.navigation")
        if role_tokens & {"healer", "nurse", "physician", "doctor", "medic", "caregiver", "midwife"}:
            skills.append("skill.care")
        if role_tokens & {"advocate", "mediator", "attorney", "defender", "organizer", "reeve"}:
            skills.append("skill.mediation")
        if profile and any(word in profile.goal.casefold() for word in ("survey", "compare", "verify", "evidence", "trace")):
            skills.append("skill.investigation")
        if self.pack.pack_id == "long_life_cultivation" and any(
            marker in role.casefold() for marker in ("cultivat", "disciple", "master", "sect")
        ):
            skills.append("skill.cultivation_practice")
        for offset, skill_id in enumerate(dict.fromkeys(skills)):
            level = 25 + ((len(role) * 3 + offset * 13) % 64)
            if tier == "player":
                level = 5 + (offset * 7)
            result[skill_id] = {"level": str(level), "experience": 20 + offset * 11, "trained_ticks": 0}
        return result

    def _grant_holding(self, owner_id: UUID, resource_id: str, quantity: str) -> dict[str, Any]:
        account_id = self._ensure_account(owner_id, resource_id, Decimal(quantity))
        existing_lot = next(
            (lot for lot in self.holdings.setdefault(owner_id, []) if lot["account_id"] == str(account_id)),
            None,
        )
        if existing_lot is None:
            lot = {
                "lot_id": str(self.entity_id("lot", owner_id, resource_id)),
                "account_id": str(account_id),
                "resource_id": resource_id,
                "quantity": quantity,
                "quality": 100,
            }
            self.holdings[owner_id].append(lot)
            return lot
        total = Decimal(existing_lot["quantity"]) + Decimal(quantity)
        existing_lot["quantity"] = _decimal_string(total)
        self._ensure_account(owner_id, resource_id, total)
        return existing_lot

    def _ensure_account(self, owner_id: UUID, resource_id: str, balance: Decimal) -> UUID:
        key = (owner_id, resource_id)
        account_id = self.accounts_by_owner_resource.get(key)
        if account_id is None:
            account_id = self.entity_id("account", owner_id, resource_id)
            self.accounts_by_owner_resource[key] = account_id
            self.resources[str(account_id)] = {
                "resource_id": resource_id,
                "balance": _decimal_string(balance),
                "owner_id": str(owner_id),
                "account_type": "holder",
            }
        else:
            existing = Decimal(self.resources[str(account_id)]["balance"])
            if balance > existing:
                self.resources[str(account_id)]["balance"] = _decimal_string(balance)
        return account_id

    def _build_organizations(self) -> None:
        for index, seed in enumerate(self.pack.polities):
            entity_id = self.polity_ids[index]
            region_index = index % _REGION_COUNT
            region_id = self.region_ids[region_index]
            members = [self.key_ids[(index * 2 + offset) % _KEY_PEOPLE] for offset in range(4)]
            self._put_organization(
                entity_id,
                "polity",
                seed.name,
                seed.public_law,
                seed.government,
                seed.resource_focus,
                region_id,
                members,
                index,
                is_polity=True,
            )
        for index, seed in enumerate(self.pack.organizations):
            entity_id = self.organization_ids[index]
            region_index = index % _REGION_COUNT
            if self.uses_feudal_occupation_profiles:
                home_region_name = FEUDAL_ORGANIZATION_HOME_REGIONS_290[seed.name]
                region_index = next(
                    region_index
                    for region_index, region in enumerate(self.pack.regions)
                    if region.name == home_region_name
                )
            region_id = self.region_ids[region_index]
            members = [self.key_ids[(index * 3 + 8 + offset) % _KEY_PEOPLE] for offset in range(5)]
            self._put_organization(
                entity_id,
                "organization",
                seed.name,
                seed.mission,
                seed.organization_type,
                seed.operating_principle,
                region_id,
                members,
                index + _POLITY_COUNT,
                is_polity=False,
            )

    def _put_organization(
        self,
        entity_id: UUID,
        kind: str,
        name: str,
        charter: str,
        government: str,
        resource_focus: str,
        location_id: UUID,
        members: list[UUID],
        ordinal: int,
        *,
        is_polity: bool,
    ) -> None:
        leader_id = members[0]
        currency_account = self._ensure_account(entity_id, self.pack.currency_id, Decimal(1000 + ordinal * 37))
        focused_resource = self._resource_for_focus(resource_focus, ordinal)
        focus_account = self._ensure_account(entity_id, focused_resource, Decimal(100 + ordinal * 9))
        succession = _succession_policy(government, is_polity=is_polity)
        components = {
            "governance": {
                "charter": charter,
                "members": [str(member_id) for member_id in members],
                "leader_id": str(leader_id),
                "offices": [
                    {
                        "office_id": "office.leader",
                        "title": "Council representative" if not is_polity else "Public steward",
                        "holder_id": str(leader_id),
                        "authority": ["authority.coordinate", "authority.represent"],
                    }
                ],
                "succession_policy": succession,
                "decision_threshold": "0.5",
                "authority_tags": ["authority.public_records", "authority.resource_coordination"],
            },
            "treasury": {
                "accounts": {
                    self.pack.currency_id: str(currency_account),
                    focused_resource: str(focus_account),
                }
            },
            "inventory": {"lots": []},
            "public_description": f"{charter} Its current operating principle is {resource_focus.replace('_', ' ')}.",
            "public_role": government.replace("_", " ").capitalize(),
        }
        if self._has_membership_recruitment():
            authority = "authority.membership_recruitment"
            clause = "charter.membership.local_service"
            eligible_roles = (
                self.pack.extra_roles if is_polity else RECRUITMENT_ROLES[self.pack.pack_id][government]
            )
            governance = components["governance"]
            governance["authority_tags"].append(authority)
            governance["charter_clause_ids"] = [clause]
            governance["membership_recruitment"] = [{
                "policy_id": f"membership.local_service.{ordinal}",
                "charter_clause_id": clause,
                "admission_rule_id": "organization.local_role_match.v1",
                "authority_tag": authority,
                "region_id": str(location_id),
                "eligible_roles": list(eligible_roles),
                "minimum_age_years": 18,
                "maximum_age_years": 64,
                "active_member_capacity": len(members) + 2,
                "max_recruits_per_period": 2,
                "selection_seed_id": str(self.entity_id("membership-recruitment", entity_id)),
            }]
        self._put_entity(entity_id, kind, name, location_id=location_id, components=components)
        if is_polity:
            goal_title = f"Maintain the public charter in {name}"
            goal_description = f"Meet the current public commitment: {charter}"
        else:
            goal_title = f"Carry out the mission of {name}"
            goal_description = f"Practice {resource_focus.replace('_', ' ')} through the stated principle: {charter}"
        self.goals.append(
            {
                "id": str(self.entity_id("goal", "organization", entity_id)),
                "owner_id": str(entity_id),
                "title": goal_title,
                "description": goal_description,
                "status": "active",
                "priority": 55 + ordinal % 40,
                "progress": 0,
                "conditions": [
                    {
                        "kind": "resource_balance_minimum",
                        "subject_id": str(entity_id),
                        "resource_id": self.pack.currency_id,
                        "quantity": "1500",
                    }
                ],
                "created_tick": 0,
                "updated_tick": 0,
            }
        )

    def _build_population_groups(self) -> None:
        for index, (region, entity_id) in enumerate(zip(self.pack.regions, self.group_ids, strict=True)):
            population = 45_000 + index * 7_500
            region_settlements = self.settlement_ids[
                index * _SETTLEMENTS_PER_REGION : (index + 1) * _SETTLEMENTS_PER_REGION
            ]
            named_count = sum(len(self.characters_at[place_id]) for place_id in region_settlements)
            population_component: dict[str, Any] = {
                "species_id": "core.human",
                "headcount": population,
                "unmodeled_headcount": population - named_count,
                "carrying_capacity": population + 15_000,
                "annual_birth_rate": "0.0095",
                "annual_death_rate": "0.0095",
                "annual_migration_rate": "0",
                "age_rates": self._population_age_rates(),
                "age_cohorts": self._initial_age_cohorts(population - named_count),
                "last_advanced_year": 0,
            }
            if self.population_mode == "sustained":
                home_place_ids = [
                    place_id for place_id in region_settlements
                    if place_id != self.discovery_site_id
                ]
                non_player_capacity = named_count - (1 if self.player_id in self.characters_at[region_settlements[0]] and index == 0 else 0)
                civilian_lifespan = 100 if self.pack.pack_id == "long_life_cultivation" else 82
                seat_policy = {
                    "non_player_seat_capacity": non_player_capacity,
                    "home_place_ids": [str(place_id) for place_id in home_place_ids],
                    "extra_given_names": list(self.pack.extra_given_names),
                    "extra_family_names": list(self.pack.extra_family_names),
                    "extra_roles": list(self.pack.extra_roles),
                    "eligible_min_age_years": 18,
                    "eligible_max_age_years": 64,
                    "lifespan_min_years": civilian_lifespan,
                    "lifespan_max_years": civilian_lifespan,
                    "seed_id": str(self._population_seat_seed_id(index)),
                    "promotion_serial": 0,
                }
                profile = self._regional_population_seat_profile(index)
                if profile is not None:
                    seat_policy["occupation_profile"] = {
                        "role_weights": dict(profile.role_weights),
                        "role_skill_levels": profile.role_skill_levels,
                    }
                    if self.uses_feudal_occupation_profiles or self.uses_cultivation_population_profiles:
                        seat_policy["extra_roles"] = [
                            role for role in self.pack.extra_roles if role in profile.role_weights
                        ]
                if (
                    self.uses_modern_occupation_profiles
                    and self.pack.version in {
                        _MODERN_CIVIC_OCCUPATION_PACK_VERSION, *_PUBLIC_WAYPOINT_PACK_VERSIONS,
                        _MODERN_CIVIC_RECRUITMENT_PACK_VERSION_210,
                    }
                ) or self.uses_feudal_occupation_profiles or self.uses_cultivation_population_profiles:
                    seat_policy["maintain_civic_occupations"] = True
                if (
                    self.uses_modern_occupation_profiles
                    and self.pack.version == _MODERN_CIVIC_RECRUITMENT_PACK_VERSION_210
                ) or self.uses_cultivation_population_profiles:
                    seat_policy["maintain_civic_recruitment_pool"] = True
                population_component["seat_policy"] = seat_policy
            self._put_entity(
                entity_id,
                "group",
                f"Residents of {region.name}",
                location_id=self.region_ids[index],
                components={
                    "population": population_component,
                    "public_description": (
                        f"Aggregate demographic baseline for {region.name}. Named characters are a small, "
                        "individually modeled subset of this regional population."
                    ),
                },
            )

    def _build_population_schedule(self) -> None:
        year_seconds = _DAY_SECONDS * self.pack.year_days
        self.scheduled_events.append(
            {
                "id": str(self.entity_id("schedule", "population-year-advance")),
                "due_tick": year_seconds,
                "phase": "environment",
                "priority": 100,
                "dependencies": [],
                "handler_id": "population.year_advance",
                "handler_version": "1.0.0",
                "status": "queued",
                "repeat_ticks": year_seconds,
                "max_runs": 600,
                "completed_runs": 0,
                "payload": {},
            }
        )

    def _build_harvest_policies(self) -> None:
        """Seed bounded monthly food and water rations for each named resident."""
        period_ticks = 30 * _DAY_SECONDS
        period_count = self.pack.year_days * 600 // 30
        schedule_id = self.entity_id("schedule", "environmental-harvest")
        for region_index, region_id in enumerate(self.region_ids):
            settlement_ids = self.settlement_ids[
                region_index * _SETTLEMENTS_PER_REGION : (region_index + 1) * _SETTLEMENTS_PER_REGION
            ]
            recipients = sorted(
                (person_id for place_id in settlement_ids for person_id in self.characters_at[place_id]),
                key=str,
            )
            raw_place = self.entities[str(region_id)]["components"]["place"]
            policies: list[dict[str, Any]] = []
            for resource_id, ration_per_period in (
                (self._food_resource(), Decimal("54")),
                (self._water_resource(), Decimal("90")),
            ):
                source_account_id = self.entity_id("environmental-source", region_id, resource_id)
                self.resources[str(source_account_id)] = {
                    "resource_id": resource_id,
                    "balance": "0",
                    "owner_id": str(region_id),
                    "account_type": "environmental_source",
                    "credit_limit": "0",
                }
                recipient_rows = []
                for person_id in recipients:
                    holder_account_id = self.accounts_by_owner_resource.get((person_id, resource_id))
                    if holder_account_id is None:
                        raise ValueError(f"Named resident {person_id} has no owned {resource_id} ration account.")
                    if not any(
                        lot["account_id"] == str(holder_account_id)
                        and lot["resource_id"] == resource_id
                        and Decimal(lot["quantity"]) > 0
                        for lot in self.holdings.get(person_id, [])
                    ):
                        raise ValueError(f"Named resident {person_id} has no FIFO-backed {resource_id} ration lot.")
                    recipient_rows.append({
                        "entity_id": str(person_id),
                        "account_id": str(holder_account_id),
                        "per_period": _decimal_string(ration_per_period),
                    })
                per_period = ration_per_period * len(recipient_rows)
                policy = {
                    "policy_id": f"harvest.{self.pack.pack_id}.{region_index}.{resource_id.rsplit('.', 1)[-1]}",
                    "resource_id": resource_id,
                    "source_account_id": str(source_account_id),
                    "period_ticks": period_ticks,
                    "per_period": _decimal_string(per_period),
                    "maximum_total": _decimal_string(per_period * period_count),
                    "issued_total": "0",
                    "last_issued_period": 0,
                    "recipients": recipient_rows,
                }
                policies.append(policy)
                self.harvest_policy_index.append({
                    "region_id": str(region_id),
                    "policy_id": policy["policy_id"],
                    "resource_id": resource_id,
                    "source_account_id": str(source_account_id),
                    "per_period": policy["per_period"],
                    "maximum_total": policy["maximum_total"],
                    "recipient_count": len(recipient_rows),
                })
            raw_place["renewable_harvests"] = policies

        self.scheduled_events.append({
            "id": str(schedule_id),
            "due_tick": period_ticks,
            "phase": "environment",
            "priority": 90,
            "dependencies": [],
            "handler_id": "environmental.harvest",
            "handler_version": "1.0.0",
            "status": "queued",
            "repeat_ticks": period_ticks,
            "max_runs": period_count,
            "completed_runs": 0,
            "payload": {},
        })

    @staticmethod
    def _population_age_rates() -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for age in range(201):
            if 20 <= age <= 39:
                birth_rate = "0.041"
            else:
                birth_rate = "0"
            if age == 0:
                death_rate = "0.01"
            elif age <= 4:
                death_rate = "0.002"
            elif age <= 49:
                death_rate = "0.001"
            elif age <= 64:
                death_rate = "0.005"
            elif age <= 74:
                death_rate = "0.02"
            elif age <= 79:
                death_rate = "0.04"
            elif age <= 89:
                death_rate = "0.08"
            elif age <= 99:
                death_rate = "0.18"
            elif age <= 119:
                death_rate = "0.4"
            elif age < 200:
                death_rate = "0.75"
            else:
                death_rate = "1"
            rows.append(
                {
                    "age_years": age,
                    "annual_birth_rate": birth_rate,
                    "annual_death_rate": death_rate,
                }
            )
        return rows

    @staticmethod
    def _initial_age_cohorts(unmodeled_headcount: int) -> list[dict[str, Any]]:
        """Spread the anonymous population across a reproducible age pyramid.

        Integer weights approximate 25% ages 0–14, 58% ages 15–64, 14% ages
        65–79, 3% ages 80–99, and a small 100+ tail. Largest remainders keep
        the exact declared anonymous headcount without float rounding.
        """
        weights = [
            1667 if age <= 14 else
            1160 if age <= 64 else
            933 if age <= 79 else
            150 if age <= 99 else
            1
            for age in range(201)
        ]
        total_weight = sum(weights)
        floors = [(unmodeled_headcount * weight) // total_weight for weight in weights]
        remainders = [(unmodeled_headcount * weight) % total_weight for weight in weights]
        remaining = unmodeled_headcount - sum(floors)
        for age in sorted(range(201), key=lambda index: (-remainders[index], index))[:remaining]:
            floors[age] += 1
        return [
            {"age_years": age, "headcount": count, "birth_remainder": "0", "death_remainder": "0"}
            for age, count in enumerate(floors)
        ]

    def _build_markets_and_production(self) -> None:
        for region_index, region in enumerate(self.pack.regions):
            region_settlements = self.settlement_ids[
                region_index * _SETTLEMENTS_PER_REGION : (region_index + 1) * _SETTLEMENTS_PER_REGION
            ]
            output_id = self._resource_for_output(region.main_output)
            for settlement_index, place_id in enumerate(region_settlements):
                local_people = self.characters_at[place_id]
                seller_id = next((person_id for person_id in local_people if person_id != self.player_id), local_people[0])
                alternate_resource = self._secondary_resource(output_id, region_index)
                region_id = self.region_ids[region_index]
                self._seed_region_transfer(region_id, seller_id, alternate_resource, Decimal("4"))
                stock_accounts = [
                    self._ensure_account(seller_id, output_id, Decimal("0")),
                    self._ensure_account(seller_id, alternate_resource, Decimal("0")),
                ]
                offers = [
                    {
                        "listing_id": str(self.entity_id("listing", place_id, resource_id)),
                        "resource_id": resource_id,
                        "stock_account_id": str(account_id),
                        "seller_id": str(seller_id),
                        "unit_price_minor": 3 + ((region_index + settlement_index) % 29),
                        "minimum_quantity": "1",
                        "active": True,
                    }
                    for resource_id, account_id in zip((output_id, alternate_resource), stock_accounts, strict=True)
                ]
                self.entities[str(place_id)]["components"]["market"] = {
                    "currency_resource_id": self.pack.currency_id,
                    "offers": offers,
                }
                input_resource = alternate_resource
                input_lot = self._seed_region_transfer(region_id, place_id, input_resource, Decimal("60"))
                self._ensure_account(place_id, output_id, Decimal("0"))
                recipe = {
                    "recipe_id": f"recipe.{self.pack.pack_id}.{region_index}.{settlement_index}",
                    "inputs": [{"resource_id": input_resource, "quantity": "2"}],
                    "outputs": [{"resource_id": output_id, "quantity": "1"}],
                    "duration_ticks": self._production_duration_ticks(6 + settlement_index % 12)
                    if self.pack.pack_id == "modern_city"
                    else self._production_duration_ticks(2 + settlement_index % 5, unit="day"),
                    "required_skill": "skill.local_knowledge",
                    "required_skill_level": "10",
                }
                self.entities[str(place_id)]["components"]["production"] = {"recipes": [recipe]}
                self._seed_production_sources(place_id, [recipe])
                self.entities[str(place_id)]["components"]["inventory"] = {
                    "lots": [input_lot],
                }

        for person_id in [self.player_id, *self.key_ids, *self.resident_ids]:
            self.entities[str(person_id)]["components"]["inventory"]["lots"] = list(
                self.holdings.get(person_id, [])
            )

    def _build_market_review_schedule(self) -> None:
        period_ticks = PRICE_REVIEW_DAYS * _DAY_SECONDS
        self.scheduled_events.append({
            "id": str(self.entity_id("schedule", "market-price-review")),
            "due_tick": period_ticks,
            "phase": "maintenance",
            "handler_id": PRICE_REVIEW_HANDLER_ID,
            "handler_version": PRICE_REVIEW_HANDLER_VERSION,
            "status": "queued",
            "repeat_ticks": period_ticks,
            "max_runs": 600 * self.pack.year_days // PRICE_REVIEW_DAYS,
        })

    def _market_pricing_config(self) -> dict[str, Any]:
        policies = []
        for market_id in sorted(map(str, self.settlement_ids)):
            market = self.entities[market_id]["components"]["market"]
            for offer in sorted(market["offers"], key=lambda row: row["listing_id"]):
                listing_id = offer["listing_id"]
                policies.append({
                    "market_id": market_id,
                    "listing_id": listing_id,
                    "policy_id": f"market.stock.{listing_id}",
                    "revision": 1,
                    "reference_price_minor": offer["unit_price_minor"],
                    "target_stock_quantity": "8",
                    "max_markup_bps": 3000,
                    "max_discount_bps": 2000,
                })
        return {
            "schema_version": PRICE_REVIEW_CONFIG_VERSION,
            "review_period_days": PRICE_REVIEW_DAYS,
            "policies": policies,
        }

    def _seed_production_sources(self, producer_id: UUID, recipes: list[dict[str, Any]]) -> None:
        """Create a finite, producer-owned output quota backed by seeded inputs."""
        quota_by_resource: dict[str, Decimal] = {}
        for recipe in recipes:
            batch_limits: list[int] = []
            for flow in recipe["inputs"]:
                input_resource = str(flow["resource_id"])
                input_account_id = self.accounts_by_owner_resource.get((producer_id, input_resource))
                if input_account_id is None:
                    raise ValueError(f"Producer {producer_id} has no seeded {input_resource} recipe input.")
                available = Decimal(self.resources[str(input_account_id)]["balance"])
                batch_limits.append(int(available // Decimal(str(flow["quantity"]))))
            maximum_batches = min(batch_limits, default=0)
            if maximum_batches <= 0:
                raise ValueError(f"Producer {producer_id} has no finite input-backed production quota.")
            for flow in recipe["outputs"]:
                resource_id = str(flow["resource_id"])
                quota = Decimal(str(flow["quantity"])) * maximum_batches
                quota_by_resource[resource_id] = quota_by_resource.get(resource_id, Decimal("0")) + quota

        for resource_id, quota in quota_by_resource.items():
            account_id = self.entity_id("production-source", producer_id, resource_id)
            if str(account_id) in self.resources:
                existing = Decimal(self.resources[str(account_id)]["credit_limit"])
                quota += existing
                self.resources[str(account_id)]["credit_limit"] = _decimal_string(quota)
            else:
                self.resources[str(account_id)] = {
                    "resource_id": resource_id,
                    "balance": "0",
                    "owner_id": str(producer_id),
                    "account_type": "production_source",
                    "credit_limit": _decimal_string(quota),
                }
            metadata = self.source_account_index[resource_id]
            metadata["aggregate_credit_limit"] = _decimal_string(
                Decimal(metadata["aggregate_credit_limit"]) + quota
            )
            metadata["producer_accounts"].append(
                {
                    "producer_id": str(producer_id),
                    "account_id": str(account_id),
                    "credit_limit": _decimal_string(quota),
                }
            )

    def _build_relationships(self) -> None:
        for place_id, residents in self.characters_at.items():
            if len(residents) < 2:
                continue
            for index, from_id in enumerate(residents):
                to_id = residents[(index + 1) % len(residents)]
                if from_id == to_id:
                    continue
                self.relations.append(
                    {
                        "id": str(self.entity_id("relation", from_id, to_id)),
                        "from_id": str(from_id),
                        "to_id": str(to_id),
                        "dimensions": {
                            "familiarity": "8",
                            "trust": "0",
                            "respect": "0",
                        },
                        "label": "Local acquaintance; trust and respect are not established at opening.",
                        "updated_tick": 0,
                        "visibility": {"policy": "public"},
                    }
                )

    def _has_membership_recruitment(self) -> bool:
        return self.population_mode == "sustained" and self.pack.version in {
            _SUSTAINED_PACK_VERSION, _INVESTIGATION_PACK_VERSION, *_CASE_PACK_VERSIONS,
        }

    def _build_opening_investigation(self) -> None:
        seed = OPENING_INVESTIGATIONS[self.pack.pack_id]
        opening = self.settlement_ids[0]
        witnesses = [person_id for person_id in self.characters_at[opening] if person_id != self.player_id]
        witness = witnesses[0]
        effective_tick = -_HOUR_SECONDS
        evidence_id = self.entity_id("evidence", "opening-investigation-archive")
        pack_source = f"{self.pack.pack_id}:{self.pack.version}"
        self._put_entity(self.entity_id("item", "opening-investigation-archive"), "item", seed.artifact_name,
                         location_id=self.discovery_site_id, components={
            "public_description": seed.artifact_description,
            "historical_evidence": {
                "evidence_id": str(evidence_id),
                "subject_id": str(opening),
                "claim": seed.archival_observation,
                "fact": {"predicate": "entity_status", "value": "active", "effective_tick": effective_tick},
                "provenance": [{"kind": "pack", "reference_id": f"{pack_source}:opening.archive"}],
            },
        })
        self.knowledge.append({
            "id": str(self.entity_id("knowledge", "opening-investigation-witness")),
            "holder_id": str(witness),
            "subject_id": str(opening),
            "claim": seed.witness_claim,
            "kind": "rumor",
            "fact": {"predicate": "entity_status", "value": "retired", "effective_tick": effective_tick},
            "disclosure_policy": "if_asked",
            "visibility": {"policy": "private"},
            "provenance": [{"kind": "pack", "reference_id": f"{pack_source}:opening.witness"}],
        })
        witness_name = self.entities[str(witness)]["name"]
        self.knowledge.append({
            "id": str(self.entity_id("knowledge", "opening-investigation-lead")),
            "owner_id": str(self.player_id),
            "subject_id": str(opening),
            "claim": f"可以向在场的 {witness_name} 打听本地过去的运营情况，也可以在附近寻找旧记录。",
            "kind": "memory",
            "visibility": {"policy": "private"},
            "provenance": [{"kind": "pack", "reference_id": f"{pack_source}:opening.lead"}],
        })
        if self.pack.version in _CASE_PACK_VERSIONS:
            draft = build_opening_case(
                self.pack.pack_id, self.pack.version, opening, self.discovery_site_id,
                witness, witnesses[1], self.entity_id,
            )
            self._put_entity(draft.anchor_id, "item", draft.anchor_name,
                             location_id=opening, components=draft.anchor_components)
            for item_id, name, location_id, components in draft.historical_items:
                self._put_entity(item_id, "item", name, location_id=location_id, components=components)
            self.knowledge.extend(draft.knowledge_records)

    def _build_goals_and_knowledge(self) -> None:
        first_target = self.settlement_ids[1]
        self.goals.append(
            {
                "id": str(self.entity_id("goal", "player")),
                "owner_id": str(self.player_id),
                "title": f"Investigate: {self.pack.scenario_hooks[0]}",
                "description": (
                    f"Begin by learning how the situation affects people in the surrounding settlements. "
                    f"Pack premise: {self.pack.scenario_hooks[0]}"
                ),
                "status": "active",
                "priority": 80,
                "progress": 0,
                "conditions": [
                    {
                        "kind": "reach_location",
                        "subject_id": str(self.player_id),
                        "target_id": str(first_target),
                    }
                ],
                "created_tick": 0,
                "updated_tick": 0,
            }
        )
        self.knowledge.extend(
            [
                {
                    "id": str(self.entity_id("knowledge", "player", "home")),
                    "owner_id": str(self.player_id),
                    "subject_id": str(self.settlement_ids[0]),
                    "claim": f"{self.entities[str(self.settlement_ids[0])]['name']} is a settlement in {self.pack.regions[0].name}.",
                    "summary": "Starting location from the world map.",
                    "confidence": "1",
                    "received_tick": 0,
                    "valid_from_tick": 0,
                    "kind": "fact",
                    "visibility": {"policy": "private"},
                },
                {
                    "id": str(self.entity_id("knowledge", "player", "calendar")),
                    "owner_id": str(self.player_id),
                    "claim": f"The local year is {self.pack.year_days} days long.",
                    "summary": "Calendar setting for this world.",
                    "confidence": "1",
                    "received_tick": 0,
                    "valid_from_tick": 0,
                    "kind": "fact",
                    "visibility": {"policy": "private"},
                },
            ]
        )

        for index, profile in enumerate(self.key_profiles):
            entity_id = self.key_ids[index]
            self._add_person_knowledge(entity_id, profile.secret, "belief", f"Private, unverified belief held by {self.key_names[index]}.", index)
            self._add_person_goal(
                entity_id,
                profile.goal_title,
                profile.goal,
                priority=65 + (index % 30),
                goal_key=f"key:{index}",
            )

        for index, entity_id in enumerate(self.resident_ids):
            role = self.resident_roles[index]
            home_place_id = UUID(self.entities[str(entity_id)]["location_id"])
            region_index = self.settlement_ids.index(home_place_id) // _SETTLEMENTS_PER_REGION
            region = self.pack.regions[region_index]
            goal_title = f"Build a dependable {region.main_output.replace('_', ' ')} reserve"
            goal_description = f"Keep a reliable supply of {region.main_output.replace('_', ' ')} while working as a {role}."
            self._add_person_knowledge(
                entity_id,
                f"You work as a {role} in a settlement whose regional output is {region.main_output.replace('_', ' ')}.",
                "memory",
                "Self-held local and work context; not a record of prior world simulation.",
                index + _KEY_PEOPLE,
            )
            self._add_person_goal(entity_id, goal_title, goal_description, priority=35 + (index % 50), goal_key=f"resident:{index}",
                                  condition={
                                      "kind": "resource_balance_minimum",
                                      "subject_id": str(entity_id),
                                      "resource_id": self._resource_for_output(region.main_output),
                                      "quantity": "20",
                                  })

    def _add_person_knowledge(
        self, owner_id: UUID, claim: str, kind: str, summary: str, ordinal: int
    ) -> None:
        self.knowledge.append(
            {
                "id": str(self.entity_id("knowledge", "person", ordinal)),
                "owner_id": str(owner_id),
                "subject_id": str(owner_id),
                "claim": claim,
                "summary": summary,
                "confidence": "0.7" if kind == "belief" else "1",
                "received_tick": 0,
                "valid_from_tick": 0,
                "kind": kind,
                "visibility": {"policy": "private"},
            }
        )

    def _add_person_goal(
        self,
        owner_id: UUID,
        title: str,
        description: str,
        *,
        priority: int,
        goal_key: str,
        condition: dict[str, Any] | None = None,
    ) -> None:
        self.goals.append(
            {
                "id": str(self.entity_id("goal", "person", goal_key)),
                "owner_id": str(owner_id),
                "title": title,
                "description": description,
                "status": "active",
                "priority": priority,
                "progress": 0,
                "conditions": [condition] if condition else [],
                "created_tick": 0,
                "updated_tick": 0,
            }
        )

    def _resource_for_output(self, output_name: str) -> str:
        output = output_name.rsplit(".", 1)[-1]
        for resource_id in self.pack.resources:
            if resource_id.rsplit(".", 1)[-1] == output:
                return resource_id
        raise ValueError(f"Pack {self.pack.pack_id!r} has no resource for output {output_name!r}.")

    def _resource_for_focus(self, focus: str, ordinal: int) -> str:
        output = focus.rsplit(".", 1)[-1]
        for resource_id in self.pack.resources:
            if resource_id.rsplit(".", 1)[-1] == output:
                return resource_id
        # Non-state organizations are indexed by their operating principle,
        # not by one exclusive commodity. Their treasury seed is diversified
        # across the pack's resource catalog by stable ordinal.
        return self.pack.resources[ordinal % len(self.pack.resources)]

    @staticmethod
    def _production_duration_ticks(duration: int, *, unit: str = "hour") -> int:
        if duration <= 0 or unit not in {"hour", "day"}:
            raise ValueError("Production durations must use a positive number of hours or days.")
        return duration * (_HOUR_SECONDS if unit == "hour" else _DAY_SECONDS)

    @staticmethod
    def _validate_built_capacity(state: WorldState) -> None:
        kinds = [entity.kind for entity in state.entities.values()]
        if kinds.count("polity") != _POLITY_COUNT or kinds.count("organization") != _ORGANIZATION_COUNT:
            raise ValueError("Built world does not meet the full country and organization profile.")
        characters = [entity for entity in state.entities.values() if entity.kind == "character"]
        resident_count = _named_resident_capacity(state.metadata.get("pack_version"), state.metadata.get("population_mode"))
        if len(characters) != 1 + _KEY_PEOPLE + resident_count:
            raise ValueError("Built world does not meet the full living character profile.")
        places = [entity for entity in state.entities.values() if entity.kind == "place"]
        if len(places) < 120:
            raise ValueError("Built world has fewer than 120 places.")


def _required_text(value: str, field: str, maximum: int) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field} must be text.")
    result = value.strip()
    if not result:
        raise ValueError(f"{field} cannot be blank.")
    if len(result) > maximum:
        raise ValueError(f"{field} must be no longer than {maximum} characters.")
    return result


def _decimal_string(value: Decimal) -> str:
    return format(value, "f")


def _words_from_role(role: str) -> list[str]:
    tokens = [token.strip(".,'") for token in role.split() if token.strip(".,'")]
    return list(dict.fromkeys(tokens[:3] + ["service", "community"]))[:5]


def _traits_from_role(role: str) -> list[str]:
    tokens = [token.strip(".,'") for token in role.split() if token.strip(".,'")]
    defaults = ["steady", "practical"]
    return list(dict.fromkeys(tokens[:2] + defaults))[:4]


def _personality_for(name_key: str, traits: list[str], values: list[str]) -> dict[str, Any]:
    digest = hashlib.sha256(name_key.casefold().encode("utf-8")).digest()
    scores = [20 + byte % 61 for byte in digest[:5]]
    openness, conscientiousness, extraversion, agreeableness, neuroticism = (
        f"0.{score:02d}" for score in scores
    )
    return {
        "openness": openness,
        "conscientiousness": conscientiousness,
        "extraversion": extraversion,
        "agreeableness": agreeableness,
        "neuroticism": neuroticism,
        "traits": traits,
        "values": values,
    }


def _appearance_for(name_key: str) -> str:
    descriptions = (
        "Keeps a close-cropped haircut and a weathered canvas coat.",
        "Wears practical layers with a carefully repaired sleeve.",
        "Has short dark hair and a small tool pouch at the belt.",
        "Wears a bright scarf and carries a marked notebook.",
        "Keeps their hair tied back and wears work-worn boots.",
        "Has a neat jacket with an old pin fastened at the collar.",
    )
    digest = hashlib.sha256(name_key.casefold().encode("utf-8")).digest()
    return descriptions[digest[5] % len(descriptions)]


def _capabilities_for(pack_id: str, role: str, is_cultivator: bool) -> list[str]:
    grants = ["world.travel", "world.social_interaction", "world.work"]
    lowered_role = role.casefold()
    if pack_id == "low_magic_feudal" and any(marker in lowered_role for marker in ("oath", "omen", "witch", "wise")):
        grants.append("magic.limited_practice")
    if is_cultivator:
        grants.extend(["cultivation.practice", "cultivation.meditate"])
    return grants


def _succession_policy(government: str, *, is_polity: bool) -> str:
    value = government.casefold()
    if any(word in value for word in ("heredit", "duchy", "barony", "estate", "principality", "lineage")):
        return "inheritance"
    if any(word in value for word in ("elect", "republic", "parliament", "vote")):
        return "election"
    if any(word in value for word in ("council", "assembly", "league", "federation", "compact", "committee")):
        return "council"
    return "appointment" if is_polity else "election"
