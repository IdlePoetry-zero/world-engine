import base64
import hashlib
from uuid import uuid4

import pytest

from world_engine.domain.errors import AccessDenied, InvalidEvent
from world_engine.domain.events import event_payload
from world_engine.domain.models import (
    AccessScope,
    CharacterCardSource,
    CommandEnvelope,
    EntityCreatedEvent,
    NewWorld,
    WorldEntity,
    WorldState,
)
from world_engine.storage.database import Database
from world_engine.storage.repository import WorldRepository


@pytest.fixture
def world_repo() -> WorldRepository:
    database = Database("sqlite:///:memory:")
    database.create_schema()
    repository = WorldRepository(database)
    try:
        yield repository
    finally:
        database.dispose()


def test_character_card_source_requires_private_authenticated_author_commit(world_repo: WorldRepository) -> None:
    tenant_id, owner_id, player_id = uuid4(), uuid4(), uuid4()
    scope = AccessScope(tenant_id=tenant_id, user_id=owner_id)
    created = world_repo.create_world(
        scope,
        NewWorld(
            title="Card source authorization",
            state=WorldState(
                player_id=player_id,
                entities={player_id: WorldEntity(id=player_id, kind="character", name="Player")},
            ),
        ),
    )
    world_id, branch_id = created.world.world_id, created.branch.branch_id

    source_bytes = b'\xef\xbb\xbf{"name":"Imported"}  \n'
    source = CharacterCardSource(
        source_format="json",
        card_version="3.0",
        original_json_base64=base64.b64encode(source_bytes).decode("ascii"),
        sha256=hashlib.sha256(source_bytes).hexdigest(),
    )

    ordinary_character = WorldEntity(id=uuid4(), kind="character", name="Ordinary")
    ordinary_event = EntityCreatedEvent(entity=ordinary_character)
    ordinary_result = world_repo.commit_command(
        scope,
        world_id,
        branch_id,
        CommandEnvelope(
            actor_id=owner_id,
            idempotency_key="ordinary-character-create",
            expected_state_version=0,
            command_type="author.create_character",
        ),
        [ordinary_event],
    )
    ordinary_record = world_repo.list_events(scope, world_id, branch_id).events[0]
    assert ordinary_record.payload == {"entity": ordinary_character.model_dump(mode="json")}
    assert "character_card_source" not in event_payload(ordinary_event)
    assert ordinary_result.last_seq == 1

    imported_character = WorldEntity(id=uuid4(), kind="character", name="Imported")
    imported_event = EntityCreatedEvent(entity=imported_character, character_card_source=source)
    generic_command = CommandEnvelope(
        actor_id=player_id,
        idempotency_key="generic-character-create-with-source",
        expected_state_version=1,
        command_type="core.tick",
    )
    with pytest.raises(AccessDenied, match="explicit character import commit"):
        world_repo.commit_command(scope, world_id, branch_id, generic_command, [imported_event])
    assert world_repo.read_world(scope, world_id, branch_id).last_seq == 1
    assert imported_character.id not in world_repo.read_world(scope, world_id, branch_id).state.entities

    forged_public_event = imported_event.model_copy(update={"visibility": {"policy": "public"}})
    with pytest.raises(InvalidEvent, match="author-only character creation"):
        world_repo.commit_command(
            scope,
            world_id,
            branch_id,
            CommandEnvelope(
                actor_id=owner_id,
                idempotency_key="public-character-card-source",
                expected_state_version=1,
                command_type="author.create_character",
            ),
            [forged_public_event],
        )
    assert world_repo.read_world(scope, world_id, branch_id).last_seq == 1

    imported_result = world_repo.commit_command(
        scope,
        world_id,
        branch_id,
        CommandEnvelope(
            actor_id=owner_id,
            idempotency_key="authenticated-character-card-import",
            expected_state_version=1,
            command_type="author.create_character",
        ),
        [imported_event],
    )
    imported_record = world_repo.list_events(scope, world_id, branch_id).events[1]
    persisted_source = imported_record.payload["character_card_source"]
    assert persisted_source["original_json_base64"] == base64.b64encode(source_bytes).decode("ascii")
    assert base64.b64decode(persisted_source["original_json_base64"]) == source_bytes
    assert persisted_source["sha256"] == hashlib.sha256(source_bytes).hexdigest()
    assert imported_result.last_seq == 2
    assert world_repo.replay(scope, world_id, branch_id).state_hash == imported_result.state_hash
