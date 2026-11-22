"""Sparse, private parentage links and actor-authorized family projections.

This module is deliberately independent of ``WorldState`` and event
registration. A caller supplies the authoritative link set and any household
memberships it has already verified; this module neither discovers membership
nor infers kinship from names or other entity attributes.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Collection, Iterable, Mapping
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, model_validator

from .components import BoundedName, Tick

MAX_KINSHIP_LINKS = 100_000


class KinshipModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        str_strip_whitespace=True,
        validate_assignment=True,
        allow_inf_nan=False,
    )


class EventKinshipSource(KinshipModel):
    kind: Literal["event"]
    event_id: UUID


class GenesisKinshipSource(KinshipModel):
    kind: Literal["genesis"]
    reference_id: BoundedName


class AuthorKinshipSource(KinshipModel):
    kind: Literal["author"]
    reference_id: BoundedName


KinshipSource = Annotated[
    EventKinshipSource | GenesisKinshipSource | AuthorKinshipSource,
    Field(discriminator="kind"),
]
KINSHIP_SOURCE_ADAPTER = TypeAdapter(KinshipSource)


class KinshipVisibility(KinshipModel):
    """A direct link is private to its endpoints, or to one verified household."""

    policy: Literal["participants", "household"] = "participants"
    household_id: UUID | None = None

    @model_validator(mode="after")
    def household_scope_is_explicit(self) -> KinshipVisibility:
        if (self.policy == "household") != (self.household_id is not None):
            raise ValueError("household kinship visibility requires exactly one household id")
        return self


class KinshipLink(KinshipModel):
    """One direct, directed parent-to-child relationship assertion."""

    link_id: UUID
    parent_id: UUID
    child_id: UUID
    relationship: Literal["birth", "adoptive"]
    created_tick: Tick
    source: KinshipSource
    visibility: KinshipVisibility = Field(default_factory=KinshipVisibility)
    ended_tick: Tick | None = None

    @model_validator(mode="after")
    def endpoints_and_interval_are_valid(self) -> KinshipLink:
        if self.parent_id == self.child_id:
            raise ValueError("a kinship link cannot name the same parent and child")
        if self.ended_tick is not None and self.ended_tick < self.created_tick:
            raise ValueError("a kinship link cannot end before it was created")
        return self


class KinshipDirectRow(KinshipModel):
    """An authorized direct edge, with its safe tick and stable link identity."""

    link_id: UUID
    parent_id: UUID
    child_id: UUID
    relationship: Literal["birth", "adoptive"]
    created_tick: Tick


class KinshipSiblingRow(KinshipModel):
    """One sibling connection proved by two authorized direct parent links."""

    sibling_id: UUID
    shared_parent_id: UUID
    subject_link_id: UUID
    sibling_link_id: UUID
    subject_relationship: Literal["birth", "adoptive"]
    sibling_relationship: Literal["birth", "adoptive"]


class KinshipInspection(KinshipModel):
    """Deterministic ego-graph view at an explicitly requested world tick."""

    actor_id: UUID
    at_tick: Tick
    parents: tuple[KinshipDirectRow, ...] = ()
    children: tuple[KinshipDirectRow, ...] = ()
    siblings: tuple[KinshipSiblingRow, ...] = ()


def validate_kinship_graph(
    links: Iterable[KinshipLink | Mapping[str, object]],
    *,
    known_party_ids: Collection[UUID] | None = None,
) -> tuple[KinshipLink, ...]:
    """Validate the sparse direct-link graph and return its stable link order.

    If ``known_party_ids`` is provided, both endpoints must occur in that set;
    this is an identity check only and does not require a living status. With no
    party index, historical or externally archived endpoint IDs are retained.
    An empty link set represents unknown lineage without fabricated ancestors.

    Historical intervals use ``[created_tick, ended_tick)``. Intervals for the
    same parent/child pair may touch at a tick but cannot overlap, including
    across the birth/adoptive types. Current ancestry cycles are rejected.
    """

    raw_links = list(links)
    if len(raw_links) > MAX_KINSHIP_LINKS:
        raise ValueError(f"kinship graph exceeds the {MAX_KINSHIP_LINKS} link limit")

    parsed: list[KinshipLink] = []
    for raw in raw_links:
        if isinstance(raw, KinshipLink):
            parsed.append(KinshipLink.model_validate(raw.model_dump(mode="python")))
        else:
            parsed.append(KinshipLink.model_validate(raw))

    link_ids = [link.link_id for link in parsed]
    if len(link_ids) != len(set(link_ids)):
        raise ValueError("kinship link IDs must be unique")

    if known_party_ids is not None:
        known = set(known_party_ids)
        for link in parsed:
            if link.parent_id not in known or link.child_id not in known:
                raise ValueError("kinship link endpoints must be known parties when an identity index is supplied")

    intervals: dict[tuple[UUID, UUID], list[KinshipLink]] = defaultdict(list)
    active_edges: dict[UUID, set[UUID]] = defaultdict(set)
    for link in parsed:
        intervals[(link.parent_id, link.child_id)].append(link)
        if link.ended_tick is None:
            active_edges[link.parent_id].add(link.child_id)

    for pair_links in intervals.values():
        ordered = sorted(pair_links, key=lambda link: (link.created_tick, str(link.link_id)))
        previous: KinshipLink | None = None
        previous_end: int | None = None
        for link in ordered:
            if previous is not None and (previous_end is None or link.created_tick < previous_end):
                if previous.ended_tick is None and link.ended_tick is None:
                    raise ValueError("active kinship links cannot duplicate a direct parent-child pair")
                raise ValueError("historical kinship intervals for one direct pair cannot overlap")
            previous = link
            previous_end = link.ended_tick

    _assert_acyclic(active_edges)
    return tuple(sorted(parsed, key=_link_sort_key))


def inspect_kinship(
    links: Iterable[KinshipLink | Mapping[str, object]],
    actor_id: UUID,
    *,
    actor_household_ids: Collection[UUID] = (),
    at_tick: int,
) -> KinshipInspection:
    """Return only direct relations and siblings proved by two visible edges.

    Household membership is a trusted input supplied by the caller, never
    inferred from a link or accepted from an unverified client assertion.
    Sibling rows require visibility of both direct parent-to-child links at
    ``at_tick``. Provenance references are intentionally omitted from the
    player-facing rows; stable link IDs and effective ticks remain available.
    """

    typed_links = validate_kinship_graph(links)
    households = set(actor_household_ids)
    if isinstance(at_tick, bool) or not isinstance(at_tick, int) or not -(2**63) <= at_tick <= 2**63 - 1:
        raise ValueError("kinship inspection tick must be a signed world tick")

    visible = tuple(
        link for link in typed_links
        if _is_active_at(link, at_tick) and _authorized(link, actor_id, households)
    )
    parents = tuple(sorted(
        (_direct_row(link) for link in visible if link.child_id == actor_id),
        key=lambda row: (str(row.parent_id), row.relationship, row.created_tick, str(row.link_id)),
    ))
    children = tuple(sorted(
        (_direct_row(link) for link in visible if link.parent_id == actor_id),
        key=lambda row: (str(row.child_id), row.relationship, row.created_tick, str(row.link_id)),
    ))

    _assert_acyclic(_adjacency(visible))
    by_parent: dict[UUID, list[KinshipLink]] = defaultdict(list)
    for link in visible:
        by_parent[link.parent_id].append(link)

    siblings: list[KinshipSiblingRow] = []
    seen: set[tuple[UUID, UUID, UUID]] = set()
    for subject_link in visible:
        if subject_link.child_id != actor_id:
            continue
        for sibling_link in by_parent.get(subject_link.parent_id, ()):
            if sibling_link.child_id == actor_id:
                continue
            identity = (sibling_link.child_id, subject_link.parent_id, sibling_link.link_id)
            if identity in seen:
                continue
            seen.add(identity)
            siblings.append(KinshipSiblingRow(
                sibling_id=sibling_link.child_id,
                shared_parent_id=subject_link.parent_id,
                subject_link_id=subject_link.link_id,
                sibling_link_id=sibling_link.link_id,
                subject_relationship=subject_link.relationship,
                sibling_relationship=sibling_link.relationship,
            ))
    siblings.sort(key=lambda row: (
        str(row.sibling_id), str(row.shared_parent_id),
        str(row.subject_link_id), str(row.sibling_link_id),
    ))

    return KinshipInspection(
        actor_id=actor_id,
        at_tick=at_tick,
        parents=parents,
        children=children,
        siblings=tuple(siblings),
    )


def _authorized(link: KinshipLink, actor_id: UUID, households: set[UUID]) -> bool:
    if actor_id in {link.parent_id, link.child_id}:
        return True
    return link.visibility.policy == "household" and link.visibility.household_id in households


def _is_active_at(link: KinshipLink, at_tick: int) -> bool:
    return link.created_tick <= at_tick and (link.ended_tick is None or at_tick < link.ended_tick)


def _direct_row(link: KinshipLink) -> KinshipDirectRow:
    return KinshipDirectRow(
        link_id=link.link_id,
        parent_id=link.parent_id,
        child_id=link.child_id,
        relationship=link.relationship,
        created_tick=link.created_tick,
    )


def _link_sort_key(link: KinshipLink) -> tuple[int, str, str, str]:
    return (link.created_tick, str(link.parent_id), str(link.child_id), str(link.link_id))


def _adjacency(links: Iterable[KinshipLink]) -> dict[UUID, set[UUID]]:
    adjacency: dict[UUID, set[UUID]] = defaultdict(set)
    for link in links:
        adjacency[link.parent_id].add(link.child_id)
    return adjacency


def _assert_acyclic(adjacency: Mapping[UUID, Collection[UUID]]) -> None:
    colors: dict[UUID, int] = {}

    def visit(node: UUID) -> None:
        color = colors.get(node, 0)
        if color == 1:
            raise ValueError("kinship parent-child ancestry cannot contain a cycle")
        if color == 2:
            return
        colors[node] = 1
        for child in sorted(adjacency.get(node, ()), key=str):
            visit(child)
        colors[node] = 2

    nodes = set(adjacency)
    for children in adjacency.values():
        nodes.update(children)
    for node in sorted(nodes, key=str):
        visit(node)
