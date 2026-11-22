"""Pure bilateral safe-conduct terms for one registered directed route.

The planners produce immutable projections only. Hosts must persist transitions
through an authorized command and still apply the ordinary route-capability,
travel-mode, restriction, visibility, and destination-policy checks.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Annotated, Literal, Protocol, TYPE_CHECKING
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .components import GovernanceComponent, PlaceComponent, PlaceRoute, is_leadership_office

if TYPE_CHECKING:
    from .models import WorldState


SAFE_CONDUCT_TAG = "diplomacy.safe_conduct"
SafeConductStatus = Literal["pending", "countered", "active", "declined", "withdrawn", "revoked"]
SafeConductOperation = Literal["ratify", "counter", "accept_counter", "decline", "withdraw", "revoke"]
SafeConductTick = Annotated[int, Field(ge=-(2**63), le=2**63 - 1)]
SafeConductRevision = Annotated[int, Field(ge=1, le=2**63 - 1)]

_PLACE_KINDS = frozenset({"place", "location"})
_ORGANIZATION_KINDS = frozenset({"organization", "polity", "faction", "company", "government"})
_PERSON_KINDS = frozenset({"character", "person"})


class SafeConductModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        str_strip_whitespace=True,
        validate_assignment=True,
        allow_inf_nan=False,
    )


class SafeConductTreaty(SafeConductModel):
    """A directed, bilateral authorization scoped to one registered route."""

    treaty_id: UUID
    proposer_organization_id: UUID
    recipient_organization_id: UUID
    proposer_actor_id: UUID
    recipient_actor_id: UUID
    route_id: UUID
    from_place_id: UUID
    to_place_id: UUID
    opened_tick: SafeConductTick
    updated_tick: SafeConductTick
    expires_at_tick: SafeConductTick
    counter_expires_at_tick: SafeConductTick | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )
    proposer_governance_revision: SafeConductRevision
    recipient_governance_revision: SafeConductRevision
    revision: SafeConductRevision = 1
    status: SafeConductStatus = "pending"

    @model_validator(mode="after")
    def valid_scope_and_lifecycle(self) -> SafeConductTreaty:
        if self.proposer_organization_id == self.recipient_organization_id:
            raise ValueError("safe conduct requires two distinct organizations")
        if self.proposer_actor_id == self.recipient_actor_id:
            raise ValueError("safe conduct requires separate proposing and ratifying actors")
        if self.from_place_id == self.to_place_id:
            raise ValueError("safe conduct requires a directed route with distinct endpoints")
        if self.expires_at_tick <= self.opened_tick:
            raise ValueError("safe conduct expiry must be after its proposal tick")
        if self.updated_tick < self.opened_tick:
            raise ValueError("safe conduct update tick cannot precede its proposal tick")
        if self.status == "countered":
            if (
                self.counter_expires_at_tick is None
                or self.counter_expires_at_tick <= self.opened_tick
                or self.counter_expires_at_tick >= self.expires_at_tick
            ):
                raise ValueError("countered safe conduct requires a shorter future counter expiry")
        elif self.counter_expires_at_tick is not None:
            if self.status in {"active", "revoked"}:
                if self.expires_at_tick != self.counter_expires_at_tick:
                    raise ValueError("accepted counter expiry must be the active treaty expiry")
            elif self.status == "withdrawn":
                if self.counter_expires_at_tick >= self.expires_at_tick:
                    raise ValueError("withdrawn counter must retain its shorter proposed expiry")
            else:
                raise ValueError("only countered, active, revoked, or withdrawn terms may retain a counter expiry")
        return self


class _Entity(Protocol):
    id: UUID
    kind: str
    name: str | None
    status: str
    location_id: UUID | None
    components: Mapping[str, object]


class _State(Protocol):
    current_tick: int
    entities: Mapping[UUID, _Entity]


class SafeConductError(ValueError):
    """A proposed safe-conduct transition does not match world state."""


def propose_safe_conduct(
    state: WorldState | _State,
    *,
    treaty_id: UUID,
    proposer_organization_id: UUID,
    recipient_organization_id: UUID,
    proposer_actor_id: UUID,
    route: PlaceRoute,
    expires_at_tick: int,
    existing_treaties: Iterable[SafeConductTreaty] = (),
) -> SafeConductTreaty:
    """Create pending terms from the source organization's current authorized leader.

    The recipient leader is derived from current state, never accepted from the
    proposal payload. The proposal itself may be made remotely; ordinary travel
    action code remains responsible for actor location and route access.
    """
    if expires_at_tick <= state.current_tick:
        raise SafeConductError("safe conduct expiry must be in the future")
    registered = registered_safe_conduct_route(state, route.from_place_id, route.route_id)
    if registered is None or registered != route:
        raise SafeConductError("safe conduct requires the exact open registered safe-conduct route")

    proposer, proposer_governance, proposer_leader = _organization_representative(
        state, proposer_organization_id,
    )
    recipient, recipient_governance, recipient_leader = _organization_representative(
        state, recipient_organization_id,
    )
    if proposer.id == recipient.id:
        raise SafeConductError("safe conduct requires two distinct organizations")
    if proposer_actor_id != proposer_leader:
        raise SafeConductError("only the source organization's represented incumbent may propose safe conduct")
    if proposer_leader == recipient_leader:
        raise SafeConductError("safe conduct requires distinct source and recipient representatives")

    source_region = _region_id_for_place(state, route.from_place_id)
    recipient_region = _region_id_for_place(state, route.to_place_id)
    if source_region == recipient_region:
        raise SafeConductError("safe conduct requires distinct source and destination regions")
    if proposer.location_id != source_region or recipient.location_id != recipient_region:
        raise SafeConductError("safe-conduct organizations must be located in their route endpoint regions")

    prior_treaties = tuple(existing_treaties)
    if any(item.treaty_id == treaty_id for item in prior_treaties):
        raise SafeConductError("safe-conduct treaty ID already exists")
    if any(
        _blocks_duplicate_term(item, state.current_tick)
        and item.proposer_organization_id == proposer_organization_id
        and item.recipient_organization_id == recipient_organization_id
        and item.route_id == route.route_id
        and item.from_place_id == route.from_place_id
        and item.to_place_id == route.to_place_id
        for item in prior_treaties
    ):
        raise SafeConductError("a live safe-conduct term already exists for this directed route and party pair")

    return SafeConductTreaty(
        treaty_id=treaty_id,
        proposer_organization_id=proposer_organization_id,
        recipient_organization_id=recipient_organization_id,
        proposer_actor_id=proposer_leader,
        recipient_actor_id=recipient_leader,
        route_id=route.route_id,
        from_place_id=route.from_place_id,
        to_place_id=route.to_place_id,
        opened_tick=state.current_tick,
        updated_tick=state.current_tick,
        expires_at_tick=expires_at_tick,
        proposer_governance_revision=proposer_governance.revision,
        recipient_governance_revision=recipient_governance.revision,
        revision=1,
        status="pending",
    )


def _blocks_duplicate_term(treaty: SafeConductTreaty, current_tick: int) -> bool:
    if treaty.status in {"pending", "active"}:
        return current_tick < treaty.expires_at_tick
    if treaty.status == "countered":
        return treaty.counter_expires_at_tick is not None and current_tick < treaty.counter_expires_at_tick
    return False


def transition_safe_conduct(
    state: WorldState | _State,
    treaty: SafeConductTreaty,
    *,
    actor_id: UUID,
    operation: SafeConductOperation,
    expected_revision: int,
    counter_expires_at_tick: int | None = None,
) -> SafeConductTreaty:
    """Counter, resolve, withdraw, or revoke one current treaty projection."""
    if expected_revision != treaty.revision:
        raise SafeConductError("safe-conduct revision does not match expected_revision")
    if state.current_tick < treaty.updated_tick:
        raise SafeConductError("world tick precedes the safe-conduct record")
    if treaty.revision >= 2**63 - 1:
        raise SafeConductError("safe-conduct revision would overflow")
    if operation != "counter" and counter_expires_at_tick is not None:
        raise SafeConductError("only a safe-conduct counter may supply a counter expiry")

    status: SafeConductStatus
    expires_at_tick = treaty.expires_at_tick
    resulting_counter_expiry = treaty.counter_expires_at_tick
    if operation == "ratify":
        _require_pending(treaty)
        if state.current_tick >= treaty.expires_at_tick:
            raise SafeConductError("expired safe conduct cannot be ratified")
        _require_pinned_live_representatives(state, treaty)
        if actor_id != treaty.recipient_actor_id:
            raise SafeConductError("only the captured recipient representative may ratify")
        status = "active"
    elif operation == "counter":
        _require_pending(treaty)
        if state.current_tick >= treaty.expires_at_tick:
            raise SafeConductError("expired safe conduct cannot be countered")
        if actor_id != treaty.recipient_actor_id:
            raise SafeConductError("only the captured recipient representative may counter safe conduct")
        _require_pinned_live_representatives(state, treaty)
        if (
            counter_expires_at_tick is None
            or counter_expires_at_tick <= state.current_tick
            or counter_expires_at_tick >= treaty.expires_at_tick
        ):
            raise SafeConductError("counter expiry must be after the current tick and before the original expiry")
        status = "countered"
        resulting_counter_expiry = counter_expires_at_tick
    elif operation == "accept_counter":
        if treaty.status != "countered" or treaty.counter_expires_at_tick is None:
            raise SafeConductError("only countered safe conduct may be accepted")
        if state.current_tick >= treaty.counter_expires_at_tick:
            raise SafeConductError("expired safe-conduct counter cannot be accepted")
        if actor_id != treaty.proposer_actor_id:
            raise SafeConductError("only the captured proposer may accept a safe-conduct counter")
        _require_pinned_live_representatives(state, treaty)
        status = "active"
        expires_at_tick = treaty.counter_expires_at_tick
    elif operation == "decline":
        _require_pending(treaty)
        _require_current_representative(state, treaty.recipient_organization_id, actor_id)
        if actor_id == treaty.proposer_actor_id:
            raise SafeConductError("safe-conduct response must be made by a separate recipient representative")
        status = "declined"
    elif operation == "withdraw":
        if treaty.status == "pending":
            _require_current_representative(state, treaty.proposer_organization_id, actor_id)
        elif treaty.status == "countered":
            # Withdrawal only closes an outstanding negotiation. The source
            # organization's current representative may clean it up even after
            # its captured counter expires or governance changes.
            _require_current_representative(state, treaty.proposer_organization_id, actor_id)
        else:
            raise SafeConductError("only pending or countered safe conduct may be withdrawn")
        status = "withdrawn"
    elif operation == "revoke":
        if treaty.status != "active":
            raise SafeConductError("only active safe conduct may be revoked")
        if not (
            _is_current_representative(state, treaty.proposer_organization_id, actor_id)
            or _is_current_representative(state, treaty.recipient_organization_id, actor_id)
        ):
            raise SafeConductError("only a current party representative may revoke safe conduct")
        status = "revoked"
    else:  # pragma: no cover - Literal guards typed callers; keep runtime fail-closed.
        raise SafeConductError("unsupported safe-conduct transition")

    return SafeConductTreaty.model_validate({
        **treaty.model_dump(mode="python"),
        "status": status,
        "revision": treaty.revision + 1,
        "updated_tick": state.current_tick,
        "expires_at_tick": expires_at_tick,
        "counter_expires_at_tick": resulting_counter_expiry,
    })


def _require_pinned_live_representatives(
    state: WorldState | _State,
    treaty: SafeConductTreaty,
) -> None:
    """Check the two captured representatives, governance pins, route and regions."""
    registered = registered_safe_conduct_route(state, treaty.from_place_id, treaty.route_id)
    if registered is None or (
        registered.from_place_id != treaty.from_place_id
        or registered.to_place_id != treaty.to_place_id
    ):
        raise SafeConductError("safe-conduct route is no longer open and registered")
    proposer, proposer_governance, proposer_leader = _organization_representative(
        state, treaty.proposer_organization_id,
    )
    recipient, recipient_governance, recipient_leader = _organization_representative(
        state, treaty.recipient_organization_id,
    )
    if proposer_leader == recipient_leader:
        raise SafeConductError("safe conduct requires distinct current representatives")
    _require_party_regions(state, treaty, proposer, recipient)
    if (
        proposer_governance.revision != treaty.proposer_governance_revision
        or recipient_governance.revision != treaty.recipient_governance_revision
        or proposer_leader != treaty.proposer_actor_id
        or recipient_leader != treaty.recipient_actor_id
    ):
        raise SafeConductError("safe-conduct governance changed before ratification")


def registered_safe_conduct_route(
    state: WorldState | _State,
    from_place_id: UUID,
    route_id: UUID,
) -> PlaceRoute | None:
    """Return one open, safe-conduct-tagged route with active visible endpoints.

    Route identifiers must resolve exactly once in the world, and the route
    returned is the stored typed value. This helper fails closed on malformed,
    hidden, ambiguous, or stale route records.
    """
    origin = state.entities.get(from_place_id)
    if (
        origin is None or origin.kind not in _PLACE_KINDS or origin.status != "active"
        or origin.components.get("hidden") is True
    ):
        return None
    try:
        origin_place = PlaceComponent.model_validate(origin.components.get("place"))
    except Exception:
        return None
    matches: list[PlaceRoute] = []
    for entity in state.entities.values():
        if entity.kind not in _PLACE_KINDS:
            continue
        try:
            place = PlaceComponent.model_validate(entity.components.get("place"))
        except Exception:
            continue
        matches.extend(route for route in place.routes if route.route_id == route_id)
    if len(matches) != 1:
        return None
    route = matches[0]
    if (
        route.from_place_id != from_place_id
        or route not in origin_place.routes
        or not route.is_open
        or SAFE_CONDUCT_TAG not in route.access_tags
    ):
        return None
    destination = state.entities.get(route.to_place_id)
    if (
        destination is None or destination.kind not in _PLACE_KINDS
        or destination.status != "active" or destination.components.get("hidden") is True
    ):
        return None
    if _region_id_for_place(state, origin.id) is None or _region_id_for_place(state, destination.id) is None:
        return None
    return route


def safe_conduct_counter_options(state: WorldState, treaty: SafeConductTreaty) -> dict[str, int]:
    """Bound a hosted representative's counteroffer to exact world-clock choices."""
    remaining = treaty.expires_at_tick - state.current_tick
    if treaty.status != "pending" or remaining <= 1:
        return {}
    choices = {"counter_half": state.current_tick + remaining // 2}
    day = state.current_tick + state.calendar.day_seconds
    if state.current_tick < day < treaty.expires_at_tick and day not in choices.values():
        choices["counter_day"] = day
    return choices


def safe_conduct_allows_route(
    state: WorldState | _State,
    actor: _Entity,
    route: PlaceRoute,
    treaties: Iterable[SafeConductTreaty],
) -> bool:
    """Whether an active source member receives only the dedicated route tag.

    This predicate does not grant travel, satisfy other route tags, bypass
    restrictions, or waive route/destination checks performed by the host.
    Active agreements are independent of later routine leader changes.
    """
    stored_actor = state.entities.get(actor.id)
    if (
        stored_actor is None
        or stored_actor.kind not in _PERSON_KINDS
        or stored_actor.status != "active"
    ):
        return False
    registered = registered_safe_conduct_route(state, route.from_place_id, route.route_id)
    if registered is None or registered != route:
        return False
    for treaty in treaties:
        if (
            treaty.status != "active"
            or state.current_tick >= treaty.expires_at_tick
            or treaty.route_id != route.route_id
            or treaty.from_place_id != route.from_place_id
            or treaty.to_place_id != route.to_place_id
        ):
            continue
        try:
            proposer, proposer_governance = _active_governed_organization(
                state, treaty.proposer_organization_id,
            )
            recipient = _active_organization(state, treaty.recipient_organization_id)
            _require_party_regions(state, treaty, proposer, recipient)
        except SafeConductError:
            continue
        if stored_actor.id in proposer_governance.members:
            return True
    return False


def _organization_representative(
    state: WorldState | _State,
    organization_id: UUID,
) -> tuple[_Entity, GovernanceComponent, UUID]:
    organization, governance = _active_governed_organization(state, organization_id)
    leader_id = governance.leader_id
    leader = state.entities.get(leader_id) if leader_id is not None else None
    if (
        leader is None
        or leader.kind not in _PERSON_KINDS
        or leader.status != "active"
        or leader_id not in governance.members
    ):
        raise SafeConductError("safe-conduct party requires an active incumbent member")
    has_representation = any(
        office.holder_id == leader_id
        and is_leadership_office(office)
        and "authority.represent" in office.authority
        and (office.term_end_tick is None or state.current_tick < office.term_end_tick)
        for office in governance.offices
    )
    if not has_representation:
        raise SafeConductError("safe-conduct incumbent must hold a leadership office with authority.represent")
    return organization, governance, leader_id


def _active_governed_organization(
    state: WorldState | _State,
    organization_id: UUID,
) -> tuple[_Entity, GovernanceComponent]:
    organization = _active_organization(state, organization_id)
    try:
        governance = GovernanceComponent.model_validate(organization.components.get("governance"))
    except Exception as exc:
        raise SafeConductError("safe-conduct party requires valid governance") from exc
    return organization, governance


def _active_organization(state: WorldState | _State, organization_id: UUID) -> _Entity:
    organization = state.entities.get(organization_id)
    if (
        organization is None
        or organization.kind not in _ORGANIZATION_KINDS
        or organization.status != "active"
        or organization.location_id is None
    ):
        raise SafeConductError("safe-conduct party must be an active located organization")
    return organization


def _current_representative_id(state: WorldState | _State, organization_id: UUID) -> UUID:
    return _organization_representative(state, organization_id)[2]


def _is_current_representative(state: WorldState | _State, organization_id: UUID, actor_id: UUID) -> bool:
    try:
        return _current_representative_id(state, organization_id) == actor_id
    except SafeConductError:
        return False


def _require_current_representative(
    state: WorldState | _State,
    organization_id: UUID,
    actor_id: UUID,
) -> None:
    current_id = _current_representative_id(state, organization_id)
    if actor_id != current_id:
        raise SafeConductError("actor is not the authorized current party representative")


def _require_party_regions(
    state: WorldState | _State,
    treaty: SafeConductTreaty,
    proposer: _Entity,
    recipient: _Entity,
) -> None:
    source_region = _region_id_for_place(state, treaty.from_place_id)
    destination_region = _region_id_for_place(state, treaty.to_place_id)
    if source_region == destination_region:
        raise SafeConductError("safe conduct requires distinct source and destination regions")
    if proposer.location_id != source_region or recipient.location_id != destination_region:
        raise SafeConductError("safe-conduct parties no longer match the route endpoint regions")


def _region_id_for_place(state: WorldState | _State, place_id: UUID) -> UUID | None:
    entity = state.entities.get(place_id)
    if (
        entity is None or entity.kind not in _PLACE_KINDS or entity.status != "active"
        or entity.components.get("hidden") is True
    ):
        return None
    try:
        place = PlaceComponent.model_validate(entity.components.get("place"))
    except Exception:
        return None
    region_id = entity.id if place.place_type == "region" else place.region_id
    if region_id is None:
        return None
    region = state.entities.get(region_id)
    if (
        region is None or region.kind not in _PLACE_KINDS or region.status != "active"
        or region.components.get("hidden") is True
    ):
        return None
    try:
        region_place = PlaceComponent.model_validate(region.components.get("place"))
    except Exception:
        return None
    if region_place.place_type != "region":
        return None
    return region_id


def _require_pending(treaty: SafeConductTreaty) -> None:
    if treaty.status != "pending":
        raise SafeConductError("only pending safe conduct can take this transition")
