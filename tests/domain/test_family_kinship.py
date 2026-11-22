from copy import deepcopy
from uuid import UUID

import pytest
from pydantic import ValidationError

from world_engine.domain.family_kinship import (
    KinshipLink,
    KinshipVisibility,
    inspect_kinship,
    validate_kinship_graph,
)


def uid(value: int) -> UUID:
    return UUID(int=value)


def link(
    link_id: int,
    parent_id: int,
    child_id: int,
    *,
    relationship: str = "birth",
    created_tick: int = 0,
    ended_tick: int | None = None,
    visibility: dict | None = None,
    source: dict | None = None,
) -> dict:
    return {
        "link_id": uid(link_id),
        "parent_id": uid(parent_id),
        "child_id": uid(child_id),
        "relationship": relationship,
        "created_tick": created_tick,
        "source": source or {"kind": "event", "event_id": uid(1000 + link_id)},
        "visibility": visibility or {"policy": "participants"},
        "ended_tick": ended_tick,
    }


def test_link_is_a_typed_two_party_fact_with_durable_origin_and_private_scope():
    fact = KinshipLink.model_validate(link(1, 10, 20, relationship="adoptive", created_tick=15))
    assert fact.parent_id == uid(10) and fact.child_id == uid(20)
    assert fact.relationship == "adoptive"
    assert fact.created_tick == 15
    assert fact.source.kind == "event" and fact.source.event_id == uid(1001)
    assert fact.visibility == KinshipVisibility(policy="participants")

    household_fact = KinshipLink.model_validate(link(
        2, 10, 21, visibility={"policy": "household", "household_id": uid(30)},
    ))
    assert household_fact.visibility.household_id == uid(30)
    with pytest.raises(ValidationError):
        KinshipLink.model_validate(link(3, 10, 22, source={"kind": "testimony", "reference_id": "rumor"}))


def test_link_rejects_self_parentage_and_visibility_requires_household_binding():
    with pytest.raises(ValidationError, match="same parent and child"):
        KinshipLink.model_validate(link(1, 10, 10))
    with pytest.raises(ValidationError, match="household kinship visibility"):
        KinshipVisibility(policy="household")
    with pytest.raises(ValidationError, match="household kinship visibility"):
        KinshipVisibility(policy="participants", household_id=uid(30))


@pytest.mark.parametrize(
    "links",
    [
        [link(1, 10, 20), link(2, 10, 20, relationship="adoptive")],
        [link(1, 10, 20), link(2, 20, 30), link(3, 30, 10)],
    ],
    ids=["duplicate-active-direct-edge", "ancestry-cycle"],
)
def test_graph_rejects_duplicate_active_edges_and_cycles(links):
    with pytest.raises(ValueError):
        validate_kinship_graph(links)


def test_graph_accepts_deceased_or_archived_parties_and_unknown_lineage():
    # The validator checks identity if a caller supplies its index, but has no
    # alive/dead precondition and retains endpoint IDs for historical archives.
    deceased_parent, child = uid(40), uid(41)
    archived = link(1, 40, 41)
    assert validate_kinship_graph([archived], known_party_ids={deceased_parent, child})[0].parent_id == deceased_parent

    inspection = inspect_kinship_graph([], actor_id=child, at_tick=500)
    assert inspection.parents == inspection.children == inspection.siblings == ()
    with pytest.raises(ValueError, match="known parties"):
        validate_kinship_graph([archived], known_party_ids={child})


def inspect_kinship_graph(links, *, actor_id, actor_household_ids=(), at_tick):
    # Small local adapter keeps the public call shape visually explicit.
    return inspect_kinship(
        links, actor_id, actor_household_ids=actor_household_ids, at_tick=at_tick,
    )


def test_historical_intervals_are_half_open_and_cannot_overlap_for_one_pair():
    earlier = link(1, 10, 20, created_tick=1, ended_tick=10)
    later = link(2, 10, 20, relationship="adoptive", created_tick=10)
    graph = validate_kinship_graph([later, earlier])
    assert len(graph) == 2
    assert inspect_kinship_graph(graph, actor_id=uid(20), at_tick=9).parents[0].relationship == "birth"
    assert inspect_kinship_graph(graph, actor_id=uid(20), at_tick=10).parents[0].relationship == "adoptive"

    overlap = link(3, 10, 20, relationship="adoptive", created_tick=9)
    with pytest.raises(ValueError, match="cannot overlap"):
        validate_kinship_graph([earlier, overlap])


def test_siblings_require_both_actor_authorized_direct_edges_and_order_is_stable():
    household_id = uid(90)
    subject_parent = link(1, 10, 20)
    subject_child = link(2, 20, 40, relationship="adoptive")
    sibling_edge = link(
        3, 10, 30, relationship="adoptive", created_tick=1,
        visibility={"policy": "household", "household_id": household_id},
    )
    another_sibling_edge = link(
        4, 10, 31, created_tick=1,
        visibility={"policy": "household", "household_id": household_id},
    )
    unrelated_household_edge = link(
        5, 50, 51, created_tick=1,
        visibility={"policy": "household", "household_id": household_id},
    )
    raw_links = [another_sibling_edge, subject_child, unrelated_household_edge, sibling_edge, subject_parent]
    before = deepcopy(raw_links)

    without_household = inspect_kinship_graph(raw_links, actor_id=uid(20), at_tick=5)
    assert [row.parent_id for row in without_household.parents] == [uid(10)]
    assert [row.child_id for row in without_household.children] == [uid(40)]
    assert without_household.siblings == ()

    with_household = inspect_kinship_graph(
        raw_links,
        actor_id=uid(20),
        actor_household_ids={household_id},
        at_tick=5,
    )
    assert [row.parent_id for row in with_household.parents] == [uid(10)]
    assert [row.child_id for row in with_household.children] == [uid(40)]
    assert [(row.sibling_id, row.shared_parent_id) for row in with_household.siblings] == [
        (uid(30), uid(10)), (uid(31), uid(10)),
    ]
    assert all(row.subject_link_id == uid(1) for row in with_household.siblings)
    assert all(row.sibling_link_id in {uid(3), uid(4)} for row in with_household.siblings)
    assert all(not hasattr(row, "source") for row in (*with_household.parents, *with_household.children, *with_household.siblings))
    assert raw_links == before

    permuted = inspect_kinship_graph(
        list(reversed(raw_links)),
        actor_id=uid(20),
        actor_household_ids={household_id},
        at_tick=5,
    )
    assert permuted.model_dump(mode="json") == with_household.model_dump(mode="json")


def test_household_authorization_is_external_and_wrong_household_does_not_reveal_sibling():
    household_id = uid(90)
    links = [
        link(1, 10, 20),
        link(2, 10, 30, visibility={"policy": "household", "household_id": household_id}),
    ]
    hidden = inspect_kinship_graph(links, actor_id=uid(20), actor_household_ids={uid(91)}, at_tick=0)
    assert hidden.siblings == ()
    authorized = inspect_kinship_graph(links, actor_id=uid(20), actor_household_ids={household_id}, at_tick=0)
    assert [row.sibling_id for row in authorized.siblings] == [uid(30)]
