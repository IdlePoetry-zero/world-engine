from uuid import uuid4

from world_engine.domain.components import ScheduledEventRecord
from world_engine.domain.models import WorldState
from world_engine.simulation.engine import SimulationEngine


def test_shipment_arrival_is_a_registered_simulation_handler():
    event = ScheduledEventRecord(
        id=uuid4(),
        due_tick=60,
        phase="delivery",
        handler_id="shipment.arrival",
        handler_version="1.0.0",
        payload={"shipment_id": str(uuid4())},
    )

    assert SimulationEngine._scheduled_handler_supported(event)


def test_shipment_spoilage_is_a_registered_dependent_delivery_handler():
    event = ScheduledEventRecord(
        id=uuid4(),
        due_tick=120,
        phase="delivery",
        handler_id="shipment.spoilage",
        handler_version="1.0.0",
        dependencies=[uuid4()],
        payload={"shipment_id": str(uuid4())},
    )

    assert SimulationEngine._scheduled_handler_supported(event)


def test_environment_schedules_run_before_same_tick_shipment_delivery():
    environment_id = uuid4()
    delivery_id = uuid4()
    state = WorldState(
        current_tick=0,
        scheduled_events=[
            ScheduledEventRecord(
                id=delivery_id,
                due_tick=60,
                phase="delivery",
                handler_id="shipment.arrival",
                handler_version="1.0.0",
                payload={"shipment_id": str(uuid4())},
            ),
            ScheduledEventRecord(
                id=environment_id,
                due_tick=60,
                phase="environment",
                handler_id="population.year_advance",
                handler_version="1.0.0",
                payload={},
            ),
        ],
    )

    boundary = SimulationEngine()._next_external_boundary(state, 60, actor_id=None)

    assert boundary is not None
    assert boundary[0] == 60
    assert boundary[2] == environment_id

