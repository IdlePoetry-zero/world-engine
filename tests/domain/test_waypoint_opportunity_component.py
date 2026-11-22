from uuid import UUID

import pytest
from pydantic import ValidationError

from world_engine.domain.components import PlaceComponent, WaypointOpportunity


FIRST_OPPORTUNITY_ID = UUID("10000000-0000-4000-8000-000000000001")
SECOND_OPPORTUNITY_ID = UUID("20000000-0000-4000-8000-000000000002")


def _opportunity(
    opportunity_id: UUID = FIRST_OPPORTUNITY_ID,
    *,
    summary: str = "A small market is open beside the road.",
    **overrides: object,
) -> WaypointOpportunity:
    return WaypointOpportunity(
        opportunity_id=opportunity_id,
        title="Border market",
        summary=summary,
        **overrides,
    )


def test_empty_waypoint_opportunities_preserve_legacy_place_serialization():
    place = PlaceComponent()

    assert place.waypoint_opportunities == []
    assert place.model_dump(mode="json") == {
        "revision": 1,
        "place_type": "site",
        "region_id": None,
        "parent_id": None,
        "capacity": None,
        "access_policy": "open",
        "terrain": None,
        "environment": {},
        "routes": [],
        "renewable_harvests": [],
    }


def test_place_rejects_duplicate_waypoint_opportunity_ids():
    opportunity = _opportunity()

    with pytest.raises(ValidationError, match="waypoint opportunity ids must be unique"):
        PlaceComponent(waypoint_opportunities=[opportunity, opportunity])


@pytest.mark.parametrize(
    ("opens_tick", "closes_tick"),
    [(100, 100), (101, 100)],
)
def test_waypoint_opportunity_rejects_empty_or_reversed_availability_ranges(opens_tick, closes_tick):
    with pytest.raises(ValidationError, match="non-empty half-open tick range"):
        _opportunity(opens_tick=opens_tick, closes_tick=closes_tick)


def test_waypoint_opportunity_uses_half_open_tick_boundaries_and_elapsed_gate():
    opportunity = _opportunity(
        opens_tick=100,
        closes_tick=200,
        minimum_itinerary_elapsed_ticks=10,
    )

    assert not opportunity.is_available(99, 10)
    assert not opportunity.is_available(100, 9)
    assert opportunity.is_available(100, 10)
    assert opportunity.is_available(199, 10)
    assert not opportunity.is_available(200, 10)


def test_waypoint_opportunities_have_stable_uuid_display_order():
    first = _opportunity(FIRST_OPPORTUNITY_ID)
    second = _opportunity(SECOND_OPPORTUNITY_ID)

    place = PlaceComponent(waypoint_opportunities=[second, first])

    assert [item.opportunity_id for item in place.waypoint_opportunities] == [
        FIRST_OPPORTUNITY_ID,
        SECOND_OPPORTUNITY_ID,
    ]
    assert len(place.model_dump(mode="json")["waypoint_opportunities"]) == 2


def test_waypoint_opportunity_summary_accepts_1000_characters_and_rejects_more():
    accepted = _opportunity(summary="x" * 1000)
    assert len(accepted.summary) == 1000

    with pytest.raises(ValidationError):
        _opportunity(summary="x" * 1001)
