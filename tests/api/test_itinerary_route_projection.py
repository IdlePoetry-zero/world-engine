from decimal import Decimal
from uuid import UUID, uuid4

from world_engine.application.worlds import WorldApplication
from world_engine.domain.components import (
    CapabilitiesComponent,
    KnowledgeRecord,
    PlaceComponent,
    PlaceRoute,
)
from world_engine.domain.models import AccessScope, NewWorld, WorldEntity, WorldState
from world_engine.domain.safe_conduct import SAFE_CONDUCT_TAG
from world_engine.storage.database import Database
from world_engine.storage.repository import WorldRepository


PLAYER_ID = UUID("30000000-0000-4000-8000-000000000003")
ORIGIN_ID = UUID("10000000-0000-4000-8000-000000000001")
KNOWN_REMOTE_ID = UUID("20000000-0000-4000-8000-000000000002")
PUBLIC_DESTINATION_ID = UUID("40000000-0000-4000-8000-000000000004")
HIDDEN_DESTINATION_ID = UUID("50000000-0000-4000-8000-000000000005")
PRIVATE_DESTINATION_ID = UUID("60000000-0000-4000-8000-000000000006")
TREATY_DESTINATION_ID = UUID("70000000-0000-4000-8000-000000000007")
UNVISITED_ORIGIN_ID = UUID("80000000-0000-4000-8000-000000000008")
UNVISITED_DESTINATION_ID = UUID("90000000-0000-4000-8000-000000000009")

ROUTE_LOCAL_ID = UUID("a0000000-0000-4000-8000-00000000000a")
ROUTE_CHAIN_ID = UUID("b0000000-0000-4000-8000-00000000000b")
ROUTE_HIDDEN_ID = UUID("c0000000-0000-4000-8000-00000000000c")
ROUTE_PRIVATE_ID = UUID("d0000000-0000-4000-8000-00000000000d")
ROUTE_TREATY_ID = UUID("e0000000-0000-4000-8000-00000000000e")
ROUTE_UNVISITED_ID = UUID("f0000000-0000-4000-8000-00000000000f")


def _route(
    route_id: UUID,
    from_place: UUID,
    to_place: UUID,
    *,
    duration: int = 10,
    access_tags: list[str] | None = None,
) -> PlaceRoute:
    return PlaceRoute(
        route_id=route_id,
        from_place_id=from_place,
        to_place_id=to_place,
        travel_ticks=duration,
        risk=Decimal("0.37"),
        access_tags=access_tags or [],
    )


def _world_state(*, observe_hidden_destination: bool = False) -> WorldState:
    origin_routes = [
        _route(ROUTE_LOCAL_ID, ORIGIN_ID, KNOWN_REMOTE_ID, duration=10),
        _route(ROUTE_HIDDEN_ID, ORIGIN_ID, HIDDEN_DESTINATION_ID, duration=11),
        _route(
            ROUTE_PRIVATE_ID,
            ORIGIN_ID,
            PRIVATE_DESTINATION_ID,
            duration=12,
            access_tags=["place.enter_private"],
        ),
        _route(
            ROUTE_TREATY_ID,
            ORIGIN_ID,
            TREATY_DESTINATION_ID,
            duration=13,
            access_tags=[SAFE_CONDUCT_TAG],
        ),
    ]
    entities = {
        PLAYER_ID: WorldEntity(
            id=PLAYER_ID,
            kind="character",
            name="Traveler",
            location_id=ORIGIN_ID,
            components={
                "capabilities": CapabilitiesComponent(grants=["world.travel"]).model_dump(mode="json"),
                "known_places": [str(KNOWN_REMOTE_ID)],
            },
        ),
        ORIGIN_ID: WorldEntity(
            id=ORIGIN_ID,
            kind="place",
            name="Home Port",
            components={"place": PlaceComponent(routes=origin_routes).model_dump(mode="json")},
        ),
        KNOWN_REMOTE_ID: WorldEntity(
            id=KNOWN_REMOTE_ID,
            kind="place",
            name="Known Waypoint",
            components={
                "place": PlaceComponent(routes=[
                    _route(ROUTE_CHAIN_ID, KNOWN_REMOTE_ID, PUBLIC_DESTINATION_ID, duration=20),
                ]).model_dump(mode="json"),
            },
        ),
        PUBLIC_DESTINATION_ID: WorldEntity(
            id=PUBLIC_DESTINATION_ID,
            kind="place",
            name="Public Harbor",
            components={"place": PlaceComponent().model_dump(mode="json")},
        ),
        HIDDEN_DESTINATION_ID: WorldEntity(
            id=HIDDEN_DESTINATION_ID,
            kind="place",
            name="Veiled Cove",
            components={"hidden": True, "place": PlaceComponent().model_dump(mode="json")},
        ),
        PRIVATE_DESTINATION_ID: WorldEntity(
            id=PRIVATE_DESTINATION_ID,
            kind="place",
            name="Private Estate",
            components={
                "place": PlaceComponent(access_policy="private").model_dump(mode="json"),
            },
        ),
        TREATY_DESTINATION_ID: WorldEntity(
            id=TREATY_DESTINATION_ID,
            kind="place",
            name="Treaty Gate",
            components={"place": PlaceComponent().model_dump(mode="json")},
        ),
        UNVISITED_ORIGIN_ID: WorldEntity(
            id=UNVISITED_ORIGIN_ID,
            kind="place",
            name="Unvisited Crossing",
            components={
                "place": PlaceComponent(routes=[
                    _route(ROUTE_UNVISITED_ID, UNVISITED_ORIGIN_ID, UNVISITED_DESTINATION_ID),
                ]).model_dump(mode="json"),
            },
        ),
        UNVISITED_DESTINATION_ID: WorldEntity(
            id=UNVISITED_DESTINATION_ID,
            kind="place",
            name="Far Point",
            components={"place": PlaceComponent().model_dump(mode="json")},
        ),
    }
    knowledge = []
    if observe_hidden_destination:
        knowledge.append(KnowledgeRecord(
            id=uuid4(),
            holder_id=PLAYER_ID,
            subject_id=HIDDEN_DESTINATION_ID,
            claim="I personally saw the cove.",
            kind="observation",
            received_tick=0,
            valid_from_tick=0,
        ))
    return WorldState(
        current_tick=0,
        player_id=PLAYER_ID,
        entities=entities,
        knowledge=knowledge,
    )


def _player_projection(tmp_path, state: WorldState) -> dict:
    database = Database(f"sqlite:///{(tmp_path / f'itinerary-projection-{uuid4()}.db').as_posix()}")
    database.create_schema()
    scope = AccessScope(tenant_id=uuid4(), user_id=uuid4())
    repository = WorldRepository(database)
    try:
        created = repository.create_world(scope, NewWorld(title="Route projection", state=state))
        application = WorldApplication(repository, None)
        return application.view_world(
            scope, created.world.world_id, created.branch.branch_id,
        )["state"]
    finally:
        database.dispose()


def test_player_itinerary_projection_chains_known_origins_without_leaking_edges(tmp_path):
    state = _world_state()
    before = state.model_dump(mode="json")

    projected = _player_projection(tmp_path, state)

    options = projected["itinerary_route_options"]
    by_route_id = {option["route_id"]: option for option in options}
    assert set(by_route_id) == {str(ROUTE_LOCAL_ID), str(ROUTE_CHAIN_ID)}
    assert by_route_id[str(ROUTE_LOCAL_ID)] == {
        "route_id": str(ROUTE_LOCAL_ID),
        "from_place_id": str(ORIGIN_ID),
        "from_place_name": "Home Port",
        "to_place_id": str(KNOWN_REMOTE_ID),
        "to_place_name": "Known Waypoint",
        "travel_ticks": "10",
    }
    assert by_route_id[str(ROUTE_CHAIN_ID)] == {
        "route_id": str(ROUTE_CHAIN_ID),
        "from_place_id": str(KNOWN_REMOTE_ID),
        "from_place_name": "Known Waypoint",
        "to_place_id": str(PUBLIC_DESTINATION_ID),
        "to_place_name": "Public Harbor",
        "travel_ticks": "20",
    }
    assert all(set(option) == {
        "route_id", "from_place_id", "from_place_name", "to_place_id", "to_place_name", "travel_ticks",
    } for option in options)
    assert all(all(isinstance(value, str) for value in option.values()) for option in options)

    # Local options keep their established shape and remain limited to the actor's current place.
    assert projected["travel_options"] == [{
        "route_id": str(ROUTE_LOCAL_ID),
        "destination_name": "Known Waypoint",
        "travel_ticks": "10",
    }]
    assert state.model_dump(mode="json") == before


def test_hidden_outward_edge_appears_only_after_a_player_observation(tmp_path):
    unobserved = _player_projection(tmp_path, _world_state())
    assert str(ROUTE_HIDDEN_ID) not in {
        option["route_id"] for option in unobserved["itinerary_route_options"]
    }

    observed = _player_projection(tmp_path, _world_state(observe_hidden_destination=True))
    observed_route = next(
        option for option in observed["itinerary_route_options"]
        if option["route_id"] == str(ROUTE_HIDDEN_ID)
    )
    assert observed_route["to_place_id"] == str(HIDDEN_DESTINATION_ID)
    assert observed_route["to_place_name"] == "Veiled Cove"
