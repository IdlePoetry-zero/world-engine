from uuid import uuid4

import pytest

from world_engine.application.world_pack_authoring import (
    WorldPackAuthoringApplication,
    WorldPackAuthoringError,
)
from world_engine.application.world_packs import WorldPackManifest, load_world_pack
from world_engine.domain.ambient_events import AmbientEventProposal, AmbientEventRecord
from world_engine.domain.models import WorldEntity, WorldState


def _manifest():
    return WorldPackManifest(pack_id="author.harbor", version="1.0.0", title="Harbor")


def _scenario(*, metadata=None, capabilities=None, additions=None):
    place_id, player_id = uuid4(), uuid4()
    place = WorldEntity(id=place_id, kind="place", name="Harbor")
    player = WorldEntity(
        id=player_id,
        kind="character",
        name="Traveler",
        location_id=place_id,
        components={"capabilities": capabilities} if capabilities is not None else {},
    )
    values = {
        "entities": {place_id: place, player_id: player},
        "player_id": player_id,
        "metadata": metadata or {},
    }
    values.update(additions or {})
    return WorldState(**values)


def test_author_world_pack_preview_validates_and_roundtrips_explicit_opening():
    scenario = _scenario()
    app = WorldPackAuthoringApplication()

    preview = app.preview(
        manifest=_manifest(),
        template={"ruleset_version": "1.0.0", "scenario": scenario},
    )
    loaded = load_world_pack(preview.archive)

    assert preview.report["can_export"] is True
    assert preview.report["roundtrip_verified"] is True
    assert preview.report["opening_count"] == 1
    assert preview.report["archive_sha256"] == preview.archive_sha256
    assert loaded.template["scenario"] == scenario.model_dump(mode="json")
    assert loaded.manifest.content_hash == preview.verified.manifest.content_hash
    exported = load_world_pack(app.export(
        manifest=_manifest(), template={"ruleset_version": "1.0.0", "scenario": scenario},
    ))
    assert exported.template == loaded.template


def test_author_export_requires_a_portable_player_opening():
    scenario = _scenario().model_dump(mode="json")
    scenario.pop("player_id")

    with pytest.raises(WorldPackAuthoringError) as error:
        WorldPackAuthoringApplication().preview(manifest=_manifest(), template={"scenario": scenario})

    assert error.value.field_errors[0]["path"] == "scenario.player_id"


def test_author_export_rejects_live_outcomes_instead_of_repackaging_them_as_opening_facts():
    scenario = _scenario()
    player_id = scenario.player_id
    place_id = scenario.entities[player_id].location_id
    scenario = scenario.model_copy(update={
        "ambient_events": [AmbientEventRecord(
            event_id=uuid4(),
            schedule_id=uuid4(),
            scope_id=place_id,
            start_tick=0,
            end_tick=86_400,
            cooldown_until_tick=0,
            proposal=AmbientEventProposal(
                kind="local_observation", place_id=place_id, initiator_id=player_id,
                title="Market notice", description="A current event.",
            ),
        )],
    })

    with pytest.raises(WorldPackAuthoringError) as error:
        WorldPackAuthoringApplication().preview(manifest=_manifest(), template={"scenario": scenario})

    assert error.value.field_errors[0]["path"] == "scenario.ambient_events"


def test_author_export_reuses_world_rules_for_capabilities():
    scenario = _scenario(
        metadata={"content_rules": {"magic": {"enabled": False}}},
        capabilities={"grants": ["magic.fire"]},
    )

    with pytest.raises(WorldPackAuthoringError) as error:
        WorldPackAuthoringApplication().preview(manifest=_manifest(), template={"scenario": scenario})

    assert error.value.field_errors[0]["path"].endswith("components.capabilities.grants.0")


def test_author_export_accepts_a_catalog_based_template_without_a_live_snapshot():
    preview = WorldPackAuthoringApplication().preview(
        manifest=_manifest(), template={"base_pack_id": "modern_city", "seed": 12},
    )

    assert preview.report["opening_count"] == 0
    assert preview.verified.template == {"base_pack_id": "modern_city", "seed": 12}


def test_author_export_rejects_non_template_branch_objects_at_the_boundary():
    with pytest.raises(WorldPackAuthoringError):
        WorldPackAuthoringApplication().preview(manifest=_manifest(), template=_scenario())
