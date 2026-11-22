"""Authored regional occupations for a future low-magic feudal 2.9.0 pack.

These profiles weight the occupations of named residents drawn from each
region's population. They do not add anyone to an organization or imply that a
resident is willing to join one; existing recruitment events remain the only
source of recorded membership.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Mapping, Sequence

from .definitions import OrganizationSeed, RegionSeed, RegionalOccupationProfile


FEUDAL_OCCUPATION_PROFILES_290_VERSION = "2.9.0"

# This is reviewed geography authored for this pack version. It is deliberately
# explicit instead of deriving placement from an organization's name.
FEUDAL_ORGANIZATION_HOME_REGIONS_290: dict[str, str] = {
    "Greyhaven Carters' Guild": "Nine Bridges Vale",
    "Rookfield Grain Factors": "South Barleylands",
    "Alderwatch Beacon Order": "Ashen March",
    "Saint Orra Infirmary": "Misted Fens",
    "Redcliff Saltwrights": "Redcliff Coast",
    "Hearthmoor Drovers' Union": "Hearthmoor",
    "Crownwood Foresters": "Old Crownwood",
    "Copper Hill Founders": "Copper Hills",
    "Pilgrim Road Hostels": "Pilgrim Road",
    "Fen Ferry Cooperative": "Misted Fens",
    "South Barley Millers": "South Barleylands",
    "White Pass Rescue Crew": "White Pass",
    "Lakeward Boatwrights": "Lakeward Cantons",
    "Vey Orchard Keepers": "The Orchard Belt",
    "Lowbridge Scribes' Hall": "Nine Bridges Vale",
    "Flintmere Miners' Lodge": "Copper Hills",
    "Three Bells Bellfounders": "Ashen March",
    "Northwood Healers' Circle": "Old Crownwood",
    "Saltmere Netmakers": "Redcliff Coast",
    "Red Field Harvest Hands": "Hearthmoor",
    "Seven Torches Couriers": "White Pass",
    "Stonegate Apothecaries": "Pilgrim Road",
    "Eastmere Boat Factors": "Lakeward Cantons",
    "Vintner's Reach Cooperage": "The Orchard Belt",
}


# Every skill id below is already used by the feudal character and population
# generation paths. Levels are modest starting aptitudes rather than claims of
# mastery or qualification.
_FEUDAL_ROLE_SKILLS_290: dict[str, dict[str, str]] = {
    "tenant farmer": {"skill.agriculture": "46", "skill.local_knowledge": "34"},
    "baker": {"skill.agriculture": "34", "skill.local_knowledge": "38"},
    "carter": {"skill.navigation": "42", "skill.local_knowledge": "34"},
    "weaver": {"skill.repair": "38", "skill.social": "30"},
    "ferryman": {"skill.navigation": "45", "skill.local_knowledge": "40"},
    "miller": {"skill.agriculture": "43", "skill.repair": "33"},
    "apprentice smith": {"skill.repair": "40"},
    "herbal gatherer": {"skill.care": "38", "skill.local_knowledge": "42"},
    "scribe": {"skill.research": "46", "skill.social": "30"},
    "watch keeper": {"skill.local_knowledge": "44", "skill.social": "32"},
    "fisher": {"skill.navigation": "39", "skill.local_knowledge": "40"},
    "midwife": {"skill.care": "48", "skill.social": "38"},
    "courier": {"skill.navigation": "45", "skill.local_knowledge": "38"},
    "cook": {"skill.local_knowledge": "38", "skill.social": "34"},
    "wheelwright": {"skill.repair": "42"},
    "shepherd": {"skill.agriculture": "42", "skill.local_knowledge": "37"},
    "barrel maker": {"skill.repair": "39"},
    "porter": {"skill.local_knowledge": "33", "skill.social": "31"},
    "tanner": {"skill.repair": "39"},
    "inn worker": {"skill.social": "36", "skill.local_knowledge": "35"},
    "boat hand": {"skill.navigation": "41", "skill.social": "30"},
    "forester": {"skill.local_knowledge": "44", "skill.agriculture": "34"},
    "market seller": {"skill.social": "42", "skill.mediation": "30"},
    "school keeper": {"skill.social": "39", "skill.research": "32"},
}


# Each region includes all recruitment-eligible occupations for its two local
# organizations at weight 4. Four other local background roles have a modest
# weight of 2, so local candidates have support without crowding out daily life.
_FEUDAL_REGIONAL_ROLE_WEIGHTS_290: dict[str, dict[str, int]] = {
    "Ashen March": {
        "watch keeper": 4,
        "forester": 4,
        "courier": 4,
        "weaver": 4,
        "apprentice smith": 4,
        "wheelwright": 4,
        "barrel maker": 4,
        "tanner": 4,
        "tenant farmer": 2,
        "baker": 2,
        "ferryman": 2,
        "shepherd": 2,
    },
    "Nine Bridges Vale": {
        "market seller": 4,
        "baker": 4,
        "weaver": 4,
        "fisher": 4,
        "scribe": 4,
        "school keeper": 4,
        "midwife": 4,
        "miller": 4,
        "carter": 2,
        "porter": 2,
        "cook": 2,
        "ferryman": 2,
    },
    "Redcliff Coast": {
        "weaver": 4,
        "apprentice smith": 4,
        "wheelwright": 4,
        "barrel maker": 4,
        "tanner": 4,
        "fisher": 2,
        "market seller": 2,
        "cook": 2,
        "boat hand": 2,
    },
    "Hearthmoor": {
        "porter": 4,
        "carter": 4,
        "tenant farmer": 4,
        "boat hand": 4,
        "miller": 4,
        "weaver": 4,
        "baker": 2,
        "ferryman": 2,
        "shepherd": 2,
        "forester": 2,
    },
    "Old Crownwood": {
        "forester": 4,
        "shepherd": 4,
        "watch keeper": 4,
        "midwife": 4,
        "cook": 4,
        "herbal gatherer": 4,
        "school keeper": 4,
        "tenant farmer": 2,
        "porter": 2,
        "weaver": 2,
        "baker": 2,
    },
    "Copper Hills": {
        "weaver": 4,
        "apprentice smith": 4,
        "wheelwright": 4,
        "barrel maker": 4,
        "tanner": 4,
        "porter": 4,
        "tenant farmer": 2,
        "carter": 2,
        "miller": 2,
        "cook": 2,
    },
    "Pilgrim Road": {
        "inn worker": 4,
        "cook": 4,
        "baker": 4,
        "midwife": 4,
        "herbal gatherer": 4,
        "scribe": 2,
        "porter": 2,
        "carter": 2,
        "tenant farmer": 2,
    },
    "Misted Fens": {
        "cook": 4,
        "midwife": 4,
        "school keeper": 4,
        "inn worker": 4,
        "carter": 4,
        "ferryman": 4,
        "boat hand": 4,
        "wheelwright": 4,
        "herbal gatherer": 2,
        "fisher": 2,
        "tenant farmer": 2,
        "baker": 2,
    },
    "South Barleylands": {
        "miller": 4,
        "tenant farmer": 4,
        "baker": 4,
        "market seller": 4,
        "porter": 4,
        "scribe": 4,
        "carter": 4,
        "weaver": 2,
        "cook": 2,
        "shepherd": 2,
        "forester": 2,
    },
    "White Pass": {
        "watch keeper": 4,
        "midwife": 4,
        "ferryman": 4,
        "boat hand": 4,
        "courier": 4,
        "carter": 4,
        "scribe": 4,
        "forester": 2,
        "shepherd": 2,
        "porter": 2,
        "baker": 2,
    },
    "Lakeward Cantons": {
        "weaver": 4,
        "apprentice smith": 4,
        "wheelwright": 4,
        "barrel maker": 4,
        "tanner": 4,
        "market seller": 4,
        "porter": 4,
        "scribe": 4,
        "carter": 4,
        "fisher": 2,
        "ferryman": 2,
        "boat hand": 2,
        "courier": 2,
    },
    "The Orchard Belt": {
        "tenant farmer": 4,
        "herbal gatherer": 4,
        "shepherd": 4,
        "weaver": 4,
        "apprentice smith": 4,
        "wheelwright": 4,
        "barrel maker": 4,
        "tanner": 4,
        "baker": 2,
        "market seller": 2,
        "cook": 2,
        "porter": 2,
    },
}

_FEUDAL_REGISTERED_SKILLS_290 = frozenset({
    "skill.agriculture",
    "skill.care",
    "skill.local_knowledge",
    "skill.mediation",
    "skill.navigation",
    "skill.repair",
    "skill.research",
    "skill.social",
})


@dataclass(frozen=True, slots=True)
class FeudalOccupationContractReport:
    """Summary of validated 2.9.0 local recruitment-feeder coverage."""

    version: str
    region_count: int
    organization_count: int
    role_count_by_region: dict[str, int]
    organizations_by_region: dict[str, tuple[str, ...]]
    feeder_roles_by_organization: dict[str, tuple[str, ...]]


def build_feudal_occupation_profiles_290() -> dict[str, RegionalOccupationProfile]:
    """Return independent, reviewed occupation profiles for all 12 regions."""
    return {
        region_name: RegionalOccupationProfile(
            role_weights=dict(role_weights),
            role_skill_levels={
                role: dict(_FEUDAL_ROLE_SKILLS_290[role])
                for role in role_weights
            },
        )
        for region_name, role_weights in _FEUDAL_REGIONAL_ROLE_WEIGHTS_290.items()
    }


def validate_feudal_occupation_contract_290(
    *,
    regions: Sequence[RegionSeed],
    organizations: Sequence[OrganizationSeed],
    profiles: Mapping[str, RegionalOccupationProfile],
    roles: Sequence[str],
    recruitment_roles: Mapping[str, Sequence[str]],
    organization_home_regions: Mapping[str, str] = FEUDAL_ORGANIZATION_HOME_REGIONS_290,
) -> FeudalOccupationContractReport:
    """Validate explicit geography, role profiles, skills, and local feeders."""
    region_names = [region.name for region in regions]
    if len(region_names) != 12 or len(set(region_names)) != len(region_names):
        raise ValueError("Pack version 2.9.0 requires 12 uniquely named regional occupation profiles.")
    if set(profiles) != set(region_names):
        missing = sorted(set(region_names) - set(profiles))
        extra = sorted(set(profiles) - set(region_names))
        raise ValueError(
            "Pack version 2.9.0 regional occupation profile coverage is invalid "
            f"(missing={missing}, extra={extra})."
        )
    organization_names = [organization.name for organization in organizations]
    if len(organization_names) != 24 or len(set(organization_names)) != len(organization_names):
        raise ValueError("Pack version 2.9.0 requires 24 uniquely named non-country organizations.")
    if set(organization_home_regions) != set(organization_names):
        missing = sorted(set(organization_names) - set(organization_home_regions))
        extra = sorted(set(organization_home_regions) - set(organization_names))
        raise ValueError(
            "Pack version 2.9.0 organization home-region coverage is invalid "
            f"(missing={missing}, extra={extra})."
        )

    valid_regions = set(region_names)
    invalid_home_regions = sorted({
        region_name
        for region_name in organization_home_regions.values()
        if region_name not in valid_regions
    })
    if invalid_home_regions:
        raise ValueError(
            "Pack version 2.9.0 organizations reference unknown home regions: "
            f"{invalid_home_regions}."
        )

    organizations_by_region_lists: dict[str, list[str]] = {name: [] for name in region_names}
    for organization in organizations:
        organizations_by_region_lists[organization_home_regions[organization.name]].append(organization.name)
    wrong_region_counts = {
        region_name: len(names)
        for region_name, names in organizations_by_region_lists.items()
        if len(names) != 2
    }
    if wrong_region_counts:
        raise ValueError(
            "Pack version 2.9.0 requires exactly two organizations per region; "
            f"counts={wrong_region_counts}."
        )

    known_roles = set(roles)
    role_count_by_region: dict[str, int] = {}
    for region_name in region_names:
        profile = profiles[region_name]
        weighted_roles = set(profile.role_weights)
        if not weighted_roles:
            raise ValueError(f"Pack version 2.9.0 region '{region_name}' has no weighted occupations.")
        unknown_roles = sorted(weighted_roles - known_roles)
        if unknown_roles:
            raise ValueError(
                f"Pack version 2.9.0 region '{region_name}' references unknown occupations: {unknown_roles}."
            )
        invalid_weights = sorted(
            role for role, weight in profile.role_weights.items()
            if type(weight) is not int or not 1 <= weight <= 1000
        )
        if invalid_weights:
            raise ValueError(
                f"Pack version 2.9.0 region '{region_name}' has invalid weights for: {invalid_weights}."
            )
        if set(profile.role_skill_levels) != weighted_roles:
            missing = sorted(weighted_roles - set(profile.role_skill_levels))
            extra = sorted(set(profile.role_skill_levels) - weighted_roles)
            raise ValueError(
                f"Pack version 2.9.0 region '{region_name}' role-to-skill mapping is incomplete "
                f"(missing={missing}, extra={extra})."
            )
        for role, skill_levels in profile.role_skill_levels.items():
            if not skill_levels:
                raise ValueError(
                    f"Pack version 2.9.0 region '{region_name}' occupation '{role}' has no authored skill mapping."
                )
            for skill_id, raw_level in skill_levels.items():
                if skill_id not in _FEUDAL_REGISTERED_SKILLS_290:
                    raise ValueError(
                        f"Pack version 2.9.0 region '{region_name}' occupation '{role}' "
                        f"references unregistered skill '{skill_id}'."
                    )
                if not isinstance(raw_level, str):
                    raise ValueError(
                        f"Pack version 2.9.0 region '{region_name}' occupation '{role}' "
                        f"has an invalid level for skill '{skill_id}'."
                    )
                try:
                    level = Decimal(raw_level)
                except (InvalidOperation, TypeError, ValueError) as exc:
                    raise ValueError(
                        f"Pack version 2.9.0 region '{region_name}' occupation '{role}' "
                        f"has an invalid level for skill '{skill_id}'."
                    ) from exc
                if not level.is_finite() or not Decimal("0") <= level <= Decimal("100"):
                    raise ValueError(
                        f"Pack version 2.9.0 region '{region_name}' occupation '{role}' "
                        f"has an out-of-range level for skill '{skill_id}'."
                    )
        role_count_by_region[region_name] = len(weighted_roles)

    feeder_roles_by_organization: dict[str, tuple[str, ...]] = {}
    for organization in organizations:
        region_name = organization_home_regions[organization.name]
        eligible_roles = recruitment_roles.get(organization.organization_type)
        if not eligible_roles:
            raise ValueError(
                f"Pack version 2.9.0 organization '{organization.name}' has no authored recruitment role set "
                f"for type '{organization.organization_type}'."
            )
        unknown_eligible_roles = sorted(set(eligible_roles) - known_roles)
        if unknown_eligible_roles:
            raise ValueError(
                f"Pack version 2.9.0 organization '{organization.name}' has recruitment roles outside the pack: "
                f"{unknown_eligible_roles}."
            )
        missing_local_roles = sorted(set(eligible_roles) - set(profiles[region_name].role_weights))
        if missing_local_roles:
            raise ValueError(
                f"Pack version 2.9.0 organization '{organization.name}' in region '{region_name}' "
                f"has no local occupation feeders for eligible roles: {missing_local_roles}."
            )
        feeder_roles_by_organization[organization.name] = tuple(sorted(set(eligible_roles)))

    return FeudalOccupationContractReport(
        version=FEUDAL_OCCUPATION_PROFILES_290_VERSION,
        region_count=len(region_names),
        organization_count=len(organization_names),
        role_count_by_region=role_count_by_region,
        organizations_by_region={
            region_name: tuple(organizations_by_region_lists[region_name])
            for region_name in region_names
        },
        feeder_roles_by_organization=feeder_roles_by_organization,
    )
