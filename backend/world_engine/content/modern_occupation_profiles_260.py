"""Fixed 2.6.0 occupational profiles for the modern sustained background.

This version keeps the 2.5 regional mix where local recruitment already has
coverage, and explicitly authors service or infrastructure roles where the
shipped organizations need them. Recruitment rules remain unchanged.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Mapping, Sequence

from .definitions import OrganizationSeed, RegionSeed, RegionalOccupationProfile


MODERN_OCCUPATION_PROFILES_260_VERSION = "2.6.0"

_REGISTERED_SKILLS = frozenset({
    "skill.care",
    "skill.local_knowledge",
    "skill.mediation",
    "skill.navigation",
    "skill.repair",
    "skill.research",
    "skill.social",
})

# These are complete profiles for each changed region. The original main-role
# weights and specialist skills are retained verbatim; the additional roles
# have modest, explicitly authored weights and matching skill maps.
MODERN_CITY_OCCUPATION_PROFILE_OVERRIDES_260: dict[str, RegionalOccupationProfile] = {
    "Harbor Ward": RegionalOccupationProfile(
        role_weights={
            "vendor": 7,
            "mechanic": 6,
            "bus driver": 4,
            "social worker": 3,
            "care worker": 3,
            "student": 2,
            "tenant advocate": 2,
            "cook": 2,
            "analyst": 2,
            "teacher": 2,
            "designer": 2,
            "data operator": 2,
        },
        role_skill_levels={
            "vendor": {"skill.social": "72"},
            "mechanic": {"skill.repair": "76"},
            "bus driver": {"skill.navigation": "73"},
            "social worker": {"skill.care": "72", "skill.mediation": "72"},
            "care worker": {"skill.care": "78", "skill.social": "68"},
            "student": {"skill.social": "55", "skill.research": "52"},
            "tenant advocate": {"skill.mediation": "76"},
            "cook": {"skill.local_knowledge": "67"},
            "analyst": {"skill.research": "78"},
            "teacher": {"skill.social": "73", "skill.research": "60"},
            "designer": {"skill.research": "61"},
            "data operator": {"skill.research": "76"},
        },
    ),
    "North Station": RegionalOccupationProfile(
        role_weights={
            "bus driver": 9,
            "dispatcher": 8,
            "delivery rider": 5,
            "tenant advocate": 2,
            "librarian": 2,
            "teacher": 2,
            "social worker": 2,
        },
        role_skill_levels={
            "bus driver": {"skill.navigation": "78"},
            "dispatcher": {"skill.navigation": "72", "skill.research": "60"},
            "delivery rider": {"skill.navigation": "70"},
            "tenant advocate": {"skill.mediation": "76"},
            "librarian": {"skill.local_knowledge": "68", "skill.research": "64"},
            "teacher": {"skill.social": "73", "skill.research": "60"},
            "social worker": {"skill.care": "72", "skill.mediation": "72"},
        },
    ),
    "Old Town": RegionalOccupationProfile(
        role_weights={
            "vendor": 8,
            "tenant advocate": 6,
            "designer": 4,
            "electrician": 2,
            "repair technician": 2,
            "dispatcher": 2,
            "mechanic": 2,
        },
        role_skill_levels={
            "vendor": {"skill.social": "74"},
            "tenant advocate": {"skill.mediation": "76"},
            "designer": {"skill.research": "61"},
            "electrician": {"skill.repair": "78"},
            "repair technician": {"skill.repair": "82"},
            "dispatcher": {"skill.navigation": "72", "skill.research": "60"},
            "mechanic": {"skill.repair": "80"},
        },
    ),
    "East Works": RegionalOccupationProfile(
        role_weights={
            "mechanic": 9,
            "repair technician": 8,
            "electrician": 7,
            "warehouse associate": 4,
            "law clerk": 2,
            "tenant advocate": 2,
            "social worker": 3,
        },
        role_skill_levels={
            "mechanic": {"skill.repair": "80"},
            "repair technician": {"skill.repair": "82"},
            "electrician": {"skill.repair": "78"},
            "warehouse associate": {"skill.local_knowledge": "62"},
            "law clerk": {"skill.research": "74", "skill.mediation": "63"},
            "tenant advocate": {"skill.mediation": "76"},
            "social worker": {"skill.care": "72", "skill.mediation": "72"},
        },
    ),
    "South Gardens": RegionalOccupationProfile(
        role_weights={
            "teacher": 7,
            "nurse": 7,
            "care worker": 7,
            "vendor": 4,
            "warehouse associate": 3,
            "dispatcher": 3,
            "delivery rider": 3,
            "data operator": 3,
        },
        role_skill_levels={
            "teacher": {"skill.social": "73", "skill.research": "60"},
            "nurse": {"skill.care": "79"},
            "care worker": {"skill.care": "77"},
            "vendor": {"skill.social": "69"},
            "warehouse associate": {"skill.local_knowledge": "72"},
            "dispatcher": {"skill.navigation": "73"},
            "delivery rider": {"skill.navigation": "74"},
            "data operator": {"skill.research": "74"},
        },
    ),
    "Airport Loop": RegionalOccupationProfile(
        role_weights={
            "warehouse associate": 8,
            "dispatcher": 7,
            "security guard": 6,
            "delivery rider": 6,
            "designer": 2,
            "care worker": 2,
            "tenant advocate": 2,
            "social worker": 3,
        },
        role_skill_levels={
            "warehouse associate": {"skill.local_knowledge": "72"},
            "dispatcher": {"skill.navigation": "73"},
            "security guard": {"skill.local_knowledge": "68", "skill.social": "60"},
            "delivery rider": {"skill.navigation": "74"},
            "designer": {"skill.research": "61"},
            "care worker": {"skill.care": "78"},
            "tenant advocate": {"skill.mediation": "76"},
            "social worker": {"skill.care": "72", "skill.mediation": "72"},
        },
    ),
}


# The six unchanged 2.5 regions are copied into this fixed version as well, so
# 2.6 genesis never depends on later edits to the legacy definitions module.
_UNCHANGED_MODERN_CITY_PROFILES_260: dict[str, RegionalOccupationProfile] = {
    "University Quarter": RegionalOccupationProfile(
        role_weights={"nurse": 10, "care worker": 9, "clinic receptionist": 8, "data operator": 7},
        role_skill_levels={
            "nurse": {"skill.care": "82", "skill.research": "62"},
            "care worker": {"skill.care": "78", "skill.social": "68"},
            "clinic receptionist": {"skill.care": "68", "skill.social": "70"},
            "data operator": {"skill.research": "76"},
        },
    ),
    "Civic Center": RegionalOccupationProfile(
        role_weights={"law clerk": 8, "social worker": 7, "analyst": 6, "data operator": 5},
        role_skill_levels={
            "law clerk": {"skill.research": "74", "skill.mediation": "63"},
            "social worker": {"skill.care": "72", "skill.mediation": "72"},
            "analyst": {"skill.research": "78"},
            "data operator": {"skill.research": "73"},
        },
    ),
    "West Exchange": RegionalOccupationProfile(
        role_weights={"analyst": 8, "bookkeeper": 7, "data operator": 6, "delivery rider": 4},
        role_skill_levels={
            "analyst": {"skill.research": "79"},
            "bookkeeper": {"skill.research": "68"},
            "data operator": {"skill.research": "74"},
            "delivery rider": {"skill.navigation": "66"},
        },
    ),
    "Riverline": RegionalOccupationProfile(
        role_weights={"bus driver": 7, "delivery rider": 7, "mechanic": 5, "vendor": 4},
        role_skill_levels={
            "bus driver": {"skill.navigation": "76"},
            "delivery rider": {"skill.navigation": "72"},
            "mechanic": {"skill.repair": "71"},
            "vendor": {"skill.social": "68"},
        },
    ),
    "Hillview": RegionalOccupationProfile(
        role_weights={"care worker": 8, "clinic receptionist": 6, "electrician": 5, "bus driver": 4},
        role_skill_levels={
            "care worker": {"skill.care": "78"},
            "clinic receptionist": {"skill.care": "65", "skill.social": "68"},
            "electrician": {"skill.repair": "70"},
            "bus driver": {"skill.navigation": "71"},
        },
    ),
    "South Market": RegionalOccupationProfile(
        role_weights={"vendor": 9, "cook": 7, "bookkeeper": 5, "delivery rider": 4},
        role_skill_levels={
            "vendor": {"skill.social": "80"},
            "cook": {"skill.local_knowledge": "67"},
            "bookkeeper": {"skill.research": "64"},
            "delivery rider": {"skill.navigation": "66"},
        },
    ),
}

MODERN_CITY_OCCUPATION_PROFILES_260 = {
    **_UNCHANGED_MODERN_CITY_PROFILES_260,
    **MODERN_CITY_OCCUPATION_PROFILE_OVERRIDES_260,
}


def build_modern_city_occupation_profiles_260() -> dict[str, RegionalOccupationProfile]:
    """Return an independent fixed-version snapshot of all 12 modern regions."""
    return dict(MODERN_CITY_OCCUPATION_PROFILES_260)


def validate_modern_city_occupation_contract_260(
    *,
    regions: Sequence[RegionSeed],
    organizations: Sequence[OrganizationSeed],
    profiles: Mapping[str, RegionalOccupationProfile],
    roles: Sequence[str],
    recruitment_roles: Mapping[str, Sequence[str]],
) -> None:
    """Validate regional profile shape and local recruitment support for all 24 organizations."""
    region_names = [region.name for region in regions]
    if len(region_names) != 12 or len(set(region_names)) != len(region_names):
        raise ValueError("Pack version 2.6.0 requires 12 uniquely named regional occupation profiles.")
    if set(profiles) != set(region_names):
        missing = sorted(set(region_names) - set(profiles))
        extra = sorted(set(profiles) - set(region_names))
        raise ValueError(
            "Pack version 2.6.0 regional occupation profile coverage is invalid "
            f"(missing={missing}, extra={extra})."
        )
    if len(organizations) != 24:
        raise ValueError("Pack version 2.6.0 requires recruitment coverage for all 24 non-polity organizations.")

    known_roles = set(roles)
    for region_name in region_names:
        profile = profiles[region_name]
        weighted_roles = set(profile.role_weights)
        if not weighted_roles:
            raise ValueError(f"Pack version 2.6.0 region '{region_name}' has no weighted occupations.")
        unknown_roles = sorted(weighted_roles - known_roles)
        if unknown_roles:
            raise ValueError(
                f"Pack version 2.6.0 region '{region_name}' references unknown occupations: {unknown_roles}."
            )
        invalid_weights = sorted(
            role for role, weight in profile.role_weights.items()
            if type(weight) is not int or weight <= 0
        )
        if invalid_weights:
            raise ValueError(
                f"Pack version 2.6.0 region '{region_name}' has non-positive integer weights for: {invalid_weights}."
            )
        if set(profile.role_skill_levels) != weighted_roles:
            missing = sorted(weighted_roles - set(profile.role_skill_levels))
            extra = sorted(set(profile.role_skill_levels) - weighted_roles)
            raise ValueError(
                f"Pack version 2.6.0 region '{region_name}' role-to-skill mapping is incomplete "
                f"(missing={missing}, extra={extra})."
            )
        for role, skill_levels in profile.role_skill_levels.items():
            if not skill_levels:
                raise ValueError(
                    f"Pack version 2.6.0 region '{region_name}' occupation '{role}' has no authored skill mapping."
                )
            for skill_id, raw_level in skill_levels.items():
                if skill_id not in _REGISTERED_SKILLS:
                    raise ValueError(
                        f"Pack version 2.6.0 region '{region_name}' occupation '{role}' "
                        f"references unregistered skill '{skill_id}'."
                    )
                if not isinstance(raw_level, str):
                    raise ValueError(
                        f"Pack version 2.6.0 region '{region_name}' occupation '{role}' "
                        f"has an invalid level for skill '{skill_id}'."
                    )
                try:
                    level = Decimal(raw_level)
                except (InvalidOperation, TypeError, ValueError) as exc:
                    raise ValueError(
                        f"Pack version 2.6.0 region '{region_name}' occupation '{role}' "
                        f"has an invalid level for skill '{skill_id}'."
                    ) from exc
                if not level.is_finite() or level < 0 or level > 100:
                    raise ValueError(
                        f"Pack version 2.6.0 region '{region_name}' occupation '{role}' "
                        f"has an out-of-range level for skill '{skill_id}'."
                    )

    for index, organization in enumerate(organizations):
        region_name = region_names[index % len(region_names)]
        eligible = recruitment_roles.get(organization.organization_type)
        if not eligible:
            raise ValueError(
                f"Pack version 2.6.0 organization '{organization.name}' has no authored recruitment role set "
                f"for type '{organization.organization_type}'."
            )
        overlap = set(eligible) & set(profiles[region_name].role_weights)
        if not overlap:
            raise ValueError(
                f"Pack version 2.6.0 organization '{organization.name}' in region '{region_name}' "
                "has no eligible occupation role in its regional profile."
            )
