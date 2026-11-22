from uuid import uuid4

import pytest

from world_engine.domain.environmental_source_events import (
    EnvironmentalSourceAccountOpenedEvent,
    apply_environmental_source_account_opened,
)
from world_engine.domain.errors import InvalidEvent
from world_engine.domain.models import ResourceAccount, WorldEntity, WorldState


def _world(*, owner_kind="place", resources=None):
    owner_id = uuid4()
    owner = WorldEntity(id=owner_id, kind=owner_kind, name="Green Reach")
    return owner_id, WorldState(entities={owner_id: owner}, resources=resources or {})


def test_environmental_source_opens_zero_and_preserves_full_state_validation():
    owner_id, state = _world()
    event = EnvironmentalSourceAccountOpenedEvent(
        account_id=uuid4(), owner_id=owner_id, resource_id="grain",
    )

    after = apply_environmental_source_account_opened(state, event)

    account = after.resources[event.account_id]
    assert account == ResourceAccount(
        resource_id="grain", balance="0", owner_id=owner_id,
        account_type="environmental_source", credit_limit="0",
    )
    assert state.resources == {}
    assert after.current_tick == state.current_tick


def test_environmental_source_rejects_duplicate_id_and_duplicate_owner_resource():
    owner_id, state = _world()
    account_id = uuid4()
    existing = ResourceAccount(
        resource_id="grain", balance="0", owner_id=owner_id,
        account_type="environmental_source", credit_limit="0",
    )
    state_with_source = state.model_copy(update={"resources": {account_id: existing}})

    with pytest.raises(InvalidEvent, match="already exists"):
        apply_environmental_source_account_opened(state_with_source, EnvironmentalSourceAccountOpenedEvent(
            account_id=account_id, owner_id=owner_id, resource_id="water",
        ))
    with pytest.raises(InvalidEvent, match="already has an environmental source"):
        apply_environmental_source_account_opened(state_with_source, EnvironmentalSourceAccountOpenedEvent(
            account_id=uuid4(), owner_id=owner_id, resource_id="grain",
        ))


def test_environmental_source_requires_existing_place_owner():
    character_id, state = _world(owner_kind="character")
    with pytest.raises(InvalidEvent, match="existing place"):
        apply_environmental_source_account_opened(state, EnvironmentalSourceAccountOpenedEvent(
            account_id=uuid4(), owner_id=character_id, resource_id="grain",
        ))


def test_environmental_source_event_is_author_only():
    owner_id, _ = _world()
    with pytest.raises(ValueError, match="author-only"):
        EnvironmentalSourceAccountOpenedEvent(
            account_id=uuid4(), owner_id=owner_id, resource_id="grain",
            visibility={"policy": "public"},
        )
