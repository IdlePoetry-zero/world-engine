from __future__ import annotations

from uuid import UUID

import pytest

from world_engine.content import ContentCatalog
from world_engine.domain.ambient_events import AmbientEventPolicy, ambient_participants
from world_engine.domain.social_contact_settlement import SOCIAL_CONTACT_HANDLER


_SOCIAL_VERSION = "2.14.0"
_BACKGROUNDS = ("modern_city", "low_magic_feudal", "long_life_cultivation")


@pytest.fixture(scope="module")
def catalog() -> ContentCatalog:
    return ContentCatalog()


def test_social_world_is_an_explicit_author_selectable_opening(catalog: ContentCatalog) -> None:
    for pack in catalog.list_packs():
        assert _SOCIAL_VERSION in pack["available_versions"]
        assert _SOCIAL_VERSION in {choice["version"] for choice in pack["opening_choices"]}


@pytest.mark.parametrize("pack_id", _BACKGROUNDS)
@pytest.mark.parametrize("population_mode", ("natural", "sustained"))
def test_social_world_214_installs_contacts_and_scoped_opportunities(
    catalog: ContentCatalog, pack_id: str, population_mode: str,
) -> None:
    draft = catalog.build_world(
        pack_id, "A Social World", "Player", seed=214, population_mode=population_mode, pack_version=_SOCIAL_VERSION,
    )
    state = draft.state

    assert draft.pack_version == state.metadata["pack_version"] == _SOCIAL_VERSION
    assert state.metadata["social_world_v1"] is True

    contacts = [row for row in state.scheduled_events if row.handler_id == SOCIAL_CONTACT_HANDLER]
    assert len(contacts) == 2
    assert {row.due_tick for row in contacts} == {state.calendar.day_seconds // 2, state.calendar.day_seconds * 4 // 5}
    assert {row.repeat_ticks for row in contacts} == {state.calendar.day_seconds}
    assert all(row.phase == "resolution" and row.max_runs >= 500 * state.calendar.year_days for row in contacts)

    ambient = [row for row in state.scheduled_events if row.handler_id == "world.ambient_event"]
    policies = [AmbientEventPolicy.model_validate(row.payload) for row in ambient]
    assert len(policies) == len(ambient) == 2
    by_scope = {policy.scope_kind: (row, policy) for row, policy in zip(ambient, policies, strict=True)}
    regional_schedule, regional_policy = by_scope["region"]
    institution_schedule, institution_policy = by_scope["organization"]

    region_id = UUID(state.metadata["content_index"]["region_ids"][0])
    organization_id = UUID(state.metadata["content_index"]["organization_ids"][0])
    assert regional_policy.scope_id == region_id
    assert institution_policy.scope_id == organization_id
    assert state.entities[region_id].kind == "place"
    organization = state.entities[organization_id]
    governance = organization.components["governance"]
    assert governance["members"]
    assert governance["leader_id"] in governance["members"]
    assert ambient_participants(state, institution_policy)

    assert regional_policy.check_every_days == 7
    assert regional_policy.chance_basis_points == 1_500
    assert regional_policy.cooldown_days == 3
    assert institution_policy.check_every_days == 14
    assert institution_policy.chance_basis_points == 1_200
    assert institution_policy.cooldown_days == 7
    assert regional_schedule.due_tick == 7 * state.calendar.day_seconds
    assert regional_schedule.repeat_ticks == 7 * state.calendar.day_seconds
    assert institution_schedule.due_tick == 14 * state.calendar.day_seconds
    assert institution_schedule.repeat_ticks == 14 * state.calendar.day_seconds
    assert all(row.phase == "decision" and row.payload for row in ambient)


@pytest.mark.parametrize("pack_id", _BACKGROUNDS)
def test_default_and_213_worlds_do_not_opt_into_social_schedules(
    catalog: ContentCatalog, pack_id: str,
) -> None:
    default = catalog.build_world(pack_id, "Legacy World", "Player", seed=13)
    previous = catalog.build_world(pack_id, "Legacy World", "Player", seed=13, pack_version="2.13.0")

    for draft in (default, previous):
        assert draft.state.metadata.get("social_world_v1") is not True
        assert all(row.handler_id != SOCIAL_CONTACT_HANDLER for row in draft.state.scheduled_events)
        assert all(row.handler_id != "world.ambient_event" for row in draft.state.scheduled_events)

    assert default.pack_version == "2.0.0"
    assert len(default.state.scheduled_events) == 2
    assert previous.pack_version == "2.13.0"
    assert {strategy.strategy_id for strategy in previous.state.organizational_strategies.values()} == {
        "internal_reserves",
    }


def test_social_world_keeps_the_213_institutional_strategy_path(catalog: ContentCatalog) -> None:
    previous = catalog.build_world("modern_city", "Institutional World", "Player", seed=13, pack_version="2.13.0")
    social = catalog.build_world("modern_city", "Institutional World", "Player", seed=13, pack_version=_SOCIAL_VERSION)

    assert {strategy.strategy_id for strategy in previous.state.organizational_strategies.values()} == {
        "internal_reserves",
    }
    assert {strategy.strategy_id for strategy in social.state.organizational_strategies.values()} == {
        "internal_reserves",
    }
    assert len(previous.state.organizational_strategies) == len(social.state.organizational_strategies) == 2
    assert previous.state.metadata["opening_investigation"].keys() == social.state.metadata["opening_investigation"].keys()
