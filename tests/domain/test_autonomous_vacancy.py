from __future__ import annotations

from uuid import UUID

import pytest

from world_engine.domain.autonomous_vacancy import (
    AUTONOMOUS_VACANCY_BALLOT_AUTHORITY,
    vacancy_opportunity,
)
from world_engine.domain.components import GovernanceComponent, OfficeRecord
from world_engine.domain.leadership_ballots import open_vacant_leadership_ballot
from world_engine.domain.models import WorldEntity, WorldState


ORGANIZATION_ID = UUID("10000000-0000-4000-8000-000000000001")
YEAR_TICKS = 100


def _state(
    *,
    count: int = 4,
    tick: int = YEAR_TICKS,
    policy: str = "election",
    authority: bool = True,
    leader: int | None = None,
    leadership_holders: tuple[int, ...] = (),
    has_leadership_office: bool = True,
    council_voters: tuple[int, ...] = (),
    inactive: tuple[int, ...] = (),
    player: int | None = None,
    open_ballots=(),
) -> tuple[WorldState, dict[int, UUID]]:
    ids = {index: UUID(int=10_000 + index) for index in range(count)}
    entities = {
        entity_id: WorldEntity(
            id=entity_id,
            kind="character",
            status="retired" if index in inactive else "active",
            name=f"Member {index}",
        )
        for index, entity_id in ids.items()
    }
    offices = []
    if has_leadership_office:
        holder_ids = tuple(ids[index] for index in leadership_holders)
        offices.append(OfficeRecord(
            office_id="office.leader",
            title="Leader",
            holder_id=holder_ids[0] if holder_ids else None,
        ))
        for offset, holder_id in enumerate(holder_ids[1:], start=1):
            offices.append(OfficeRecord(
                office_id=f"office.leadership.{offset}",
                title=f"Deputy {offset}",
                holder_id=holder_id,
                authority=["organization.lead"],
            ))
    for index in council_voters:
        offices.append(OfficeRecord(
            office_id=f"office.council.{index}",
            title=f"Council {index}",
            holder_id=ids[index],
            authority=["authority.succession_vote"],
        ))
    governance = GovernanceComponent(
        revision=7,
        charter="Private charter language must never appear in an opportunity.",
        members=list(ids.values()),
        leader_id=ids[leader] if leader is not None else None,
        offices=offices,
        succession_policy=policy,
        authority_tags=[AUTONOMOUS_VACANCY_BALLOT_AUTHORITY] if authority else [],
    )
    entities[ORGANIZATION_ID] = WorldEntity(
        id=ORGANIZATION_ID,
        kind="organization",
        name="Council House",
        components={"governance": governance.model_dump(mode="json")},
    )
    return WorldState(
        current_tick=tick,
        calendar={"day_seconds": 10, "year_days": 10},
        entities=entities,
        player_id=ids[player] if player is not None else None,
        leadership_ballots=list(open_ballots),
    ), ids


def test_opportunity_requires_authority_and_begins_only_after_a_full_year():
    state, _ = _state(tick=YEAR_TICKS - 1)
    assert vacancy_opportunity(state, ORGANIZATION_ID) is None

    state, _ = _state(tick=YEAR_TICKS, authority=False)
    assert vacancy_opportunity(state, ORGANIZATION_ID) is None

    state, _ = _state(tick=YEAR_TICKS)
    before = state.model_dump(mode="python")
    opportunity = vacancy_opportunity(state, ORGANIZATION_ID)

    assert opportunity is not None
    assert opportunity.period_index == 1
    assert opportunity.governance_revision == 7
    assert opportunity.model_dump(mode="json").keys() == {
        "organization_id", "governance_revision", "period_index", "proposer_id",
        "candidate_ids", "eligible_voter_ids",
    }
    assert "Private charter language" not in str(opportunity.model_dump(mode="json"))
    assert state.model_dump(mode="python") == before

    after_two_years, _ = _state(tick=2 * YEAR_TICKS + 17)
    assert vacancy_opportunity(after_two_years, ORGANIZATION_ID).period_index == 2


@pytest.mark.parametrize(
    "updates",
    [
        {"leader": 0},
        {"has_leadership_office": False},
        {"leadership_holders": (0, 1)},
        {"policy": "appointment"},
        {"policy": "inheritance"},
        {"policy": "none"},
        {"inactive": (0, 1, 2, 3)},
    ],
)
def test_incompatible_authority_leadership_or_voter_state_has_no_opportunity(updates):
    state, _ = _state(**updates)
    assert vacancy_opportunity(state, ORGANIZATION_ID) is None


def test_an_existing_open_ballot_suppresses_another_opportunity():
    state, ids = _state()
    existing = open_vacant_leadership_ballot(
        state,
        ballot_id=UUID("20000000-0000-4000-8000-000000000001"),
        organization_id=ORGANIZATION_ID,
        nominee_id=ids[1],
        actor_id=ids[0],
        existing_ballots=(),
    )
    state = state.model_copy(update={"leadership_ballots": [existing]})

    assert vacancy_opportunity(state, ORGANIZATION_ID) is None


def test_voters_come_from_registered_council_authority_and_controlled_voters_block():
    state, ids = _state(
        count=5,
        policy="council",
        council_voters=(1, 3),
        player=2,
    )
    opportunity = vacancy_opportunity(state, ORGANIZATION_ID)

    assert opportunity is not None
    assert opportunity.eligible_voter_ids == tuple(sorted((ids[1], ids[3]), key=str))
    assert ids[2] not in opportunity.eligible_voter_ids
    assert ids[2] not in opportunity.candidate_ids
    assert opportunity.proposer_id == min((ids[1], ids[3]), key=str)

    player_voter, _ = _state(
        count=5,
        policy="council",
        council_voters=(1, 3),
        player=1,
    )
    assert vacancy_opportunity(player_voter, ORGANIZATION_ID) is None
    controlled_voter, ids = _state(count=5, policy="council", council_voters=(1, 3))
    assert vacancy_opportunity(
        controlled_voter, ORGANIZATION_ID, controlled_actor_ids=frozenset({ids[3]}),
    ) is None


def test_consistent_office_holder_is_the_only_candidate_and_controlled_nominees_are_skipped():
    council_state, ids = _state(
        count=4,
        policy="council",
        council_voters=(0, 1),
        leadership_holders=(2,),
    )
    opportunity = vacancy_opportunity(council_state, ORGANIZATION_ID)
    assert opportunity is not None
    assert opportunity.candidate_ids == (ids[2],)

    controlled_holder = vacancy_opportunity(
        council_state, ORGANIZATION_ID, controlled_actor_ids=frozenset({ids[2]}),
    )
    assert controlled_holder is None

    inactive_holder, _ = _state(
        count=4,
        policy="council",
        council_voters=(0, 1),
        leadership_holders=(2,),
        inactive=(2,),
    )
    assert vacancy_opportunity(inactive_holder, ORGANIZATION_ID) is None


def test_candidate_order_is_stable_and_capped_at_thirty_two():
    state, ids = _state(count=40)
    opportunity = vacancy_opportunity(state, ORGANIZATION_ID)

    assert opportunity is not None
    assert len(opportunity.candidate_ids) == 32
    assert opportunity.candidate_ids == tuple(sorted(
        ids.values(),
        key=str,
    )[:32])
    assert opportunity.eligible_voter_ids == tuple(sorted(ids.values(), key=str))
