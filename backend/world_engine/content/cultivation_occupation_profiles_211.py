"""Regional replacement-seat occupations for sustained cultivation 2.11.0."""

from __future__ import annotations

from .definitions import RegionalOccupationProfile


CULTIVATION_OCCUPATION_PROFILES_211_VERSION = "2.11.0"

# Each region retains all admission roles of its two local organizations.
# The other weights preserve ordinary farming, care, travel, study and household
# work. Cloudstep's light boat-hand weighting supports its registered river and
# pass handoffs without displacing the region's courier and training roles.
_REGIONAL_ROLE_WEIGHTS_211: dict[str, dict[str, int]] = {
    "Cloudstep Range": {
        "courier": 7, "boat hand": 3, "traveler": 4,
        "disciple": 4, "teacher": 3, "apprentice": 3, "watch guard": 3,
        "porter": 3, "hostel keeper": 2, "cook": 2, "scribe": 2, "valley farmer": 2,
    },
    "Jade River Basin": {
        "water steward": 7, "boat hand": 5, "valley farmer": 6,
        "forge worker": 2, "boatwright": 2, "miner": 2,
        "granary worker": 4, "cook": 3, "gardener": 3, "market vendor": 2,
        "courier": 2, "porter": 2,
    },
    "Red Lantern Coast": {
        "market vendor": 5, "porter": 4, "scribe": 3, "boat hand": 5,
        "forge worker": 3, "boatwright": 5, "weaver": 3, "apprentice": 3,
        "healer": 2, "cook": 3, "courier": 2, "traveler": 2,
    },
    "Quiet Pine Sea": {
        "water steward": 3, "gardener": 5, "watch guard": 3, "herb gatherer": 7,
        "healer": 3, "pharmacist": 4,
        "valley farmer": 4, "cook": 3, "traveler": 2, "porter": 2,
        "scribe": 2, "courier": 2,
    },
    "Stone Meridian": {
        "teacher": 4, "scribe": 4, "healer": 2, "record keeper": 5,
        "miner": 5, "porter": 3, "forge worker": 5, "weaver": 3,
        "valley farmer": 3, "cook": 2, "apprentice": 3, "market vendor": 2,
    },
    "Hundred Bell Plain": {
        "courier": 5, "porter": 4, "boat hand": 3, "record keeper": 4,
        "market vendor": 5,
        "hostel keeper": 3, "cook": 3, "traveler": 3, "valley farmer": 3,
        "scribe": 3, "teacher": 2, "granary worker": 2,
    },
    "Frostgate Peaks": {
        "watch guard": 5, "healer": 4, "boat hand": 2, "porter": 4,
        "hostel keeper": 4, "cook": 3, "traveler": 4, "courier": 4,
        "forge worker": 2, "miner": 3, "apprentice": 2, "herb gatherer": 2,
    },
    "Silver Reed Marsh": {
        "healer": 5, "pharmacist": 5, "herb gatherer": 7,
        "gardener": 4, "water steward": 5, "valley farmer": 3,
        "cook": 3, "traveler": 2, "boat hand": 2, "courier": 2,
        "record keeper": 2, "hostel keeper": 2,
    },
    "Sunward Terrace": {
        "valley farmer": 7, "cook": 4, "granary worker": 7, "gardener": 5,
        "porter": 3, "miner": 2, "boat hand": 2,
        "water steward": 4, "market vendor": 3, "weaver": 2,
        "traveler": 2, "teacher": 2,
    },
    "Mirror Lake Domain": {
        "scribe": 6, "teacher": 6, "record keeper": 6, "disciple": 4,
        "market vendor": 2, "cook": 2, "traveler": 2, "gardener": 2,
        "hostel keeper": 2, "courier": 2, "healer": 2, "valley farmer": 2,
    },
    "Black Anvil Ridge": {
        "forge worker": 7, "boatwright": 5, "weaver": 4, "apprentice": 6,
        "miner": 5, "porter": 3, "cook": 2, "market vendor": 2,
        "courier": 2, "teacher": 2, "record keeper": 2, "valley farmer": 2,
    },
    "Dawn Veil Valley": {
        "healer": 5, "herb gatherer": 4, "pharmacist": 4, "cook": 4,
        "record keeper": 4, "scribe": 3, "courier": 2,
        "valley farmer": 3, "gardener": 3, "hostel keeper": 2,
        "teacher": 2, "traveler": 2,
    },
}

# These are existing cultivation seed skill IDs. Values are entry aptitudes,
# not credentials or proof of qualification for a position.
_ROLE_SKILLS_211: dict[str, dict[str, str]] = {
    "valley farmer": {"skill.agriculture": "56", "skill.local_knowledge": "38"},
    "disciple": {"skill.cultivation_practice": "52", "skill.social": "34"},
    "courier": {"skill.navigation": "50", "skill.local_knowledge": "38"},
    "herb gatherer": {"skill.agriculture": "48", "skill.local_knowledge": "46"},
    "boat hand": {"skill.navigation": "46", "skill.local_knowledge": "40"},
    "scribe": {"skill.research": "50", "skill.social": "34"},
    "forge worker": {"skill.repair": "50", "skill.local_knowledge": "34"},
    "healer": {"skill.care": "54", "skill.social": "36"},
    "market vendor": {"skill.social": "48", "skill.mediation": "32"},
    "water steward": {"skill.agriculture": "46", "skill.local_knowledge": "44"},
    "hostel keeper": {"skill.social": "44", "skill.local_knowledge": "40"},
    "cook": {"skill.local_knowledge": "44", "skill.social": "36"},
    "apprentice": {"skill.repair": "38", "skill.cultivation_practice": "34"},
    "porter": {"skill.local_knowledge": "38", "skill.social": "32"},
    "teacher": {"skill.social": "48", "skill.research": "38"},
    "weaver": {"skill.repair": "42", "skill.social": "34"},
    "miner": {"skill.repair": "44", "skill.local_knowledge": "38"},
    "boatwright": {"skill.repair": "48", "skill.navigation": "36"},
    "record keeper": {"skill.research": "50", "skill.social": "36"},
    "watch guard": {"skill.local_knowledge": "46", "skill.social": "38"},
    "granary worker": {"skill.agriculture": "48", "skill.local_knowledge": "38"},
    "gardener": {"skill.agriculture": "48", "skill.care": "34"},
    "traveler": {"skill.navigation": "42", "skill.local_knowledge": "40"},
    "pharmacist": {"skill.care": "48", "skill.research": "40"},
}


def build_cultivation_occupation_profiles_211() -> dict[str, RegionalOccupationProfile]:
    """Return fresh regional profiles used only when replacing named seats."""
    return {
        region_name: RegionalOccupationProfile(
            role_weights=dict(role_weights),
            role_skill_levels={role: dict(_ROLE_SKILLS_211[role]) for role in role_weights},
        )
        for region_name, role_weights in _REGIONAL_ROLE_WEIGHTS_211.items()
    }
