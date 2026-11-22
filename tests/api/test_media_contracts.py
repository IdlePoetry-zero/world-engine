from uuid import uuid4

import pytest
from pydantic import ValidationError
from hashlib import sha256

from world_engine.application.media_contracts import (
    AssetScope,
    AuthorizedMediaSource,
    GeneratedMedia,
    MediaManifest,
    MediaRegistry,
    MediaRequest,
    MediaSubscription,
    MediaSubscriptionMatch,
)
from world_engine.domain.models import EventRecord, utc_now


def test_media_plugin_cannot_override_host_audience_or_request_binding():
    audience = AssetScope(policy="participants", subject_ids=(uuid4(),))
    request = MediaRequest(
        request_id=uuid4(), world_id=uuid4(), branch_id=uuid4(), source_event_ids=(uuid4(),),
        kind="speech", authorized_description="The archivist greets the visitor.",
        visibility=audience, idempotency_key="narration-voice",
    )
    receipt = {
        "request_id": request.request_id, "content_hash": "a" * 64,
        "content_type": "audio/wav", "byte_size": 2048,
    }
    GeneratedMedia.model_validate(receipt).validate_for(request, maximum_bytes=2048)
    with pytest.raises(ValidationError):
        GeneratedMedia.model_validate(receipt | {"visibility": {"policy": "public"}})
    with pytest.raises(ValueError):
        GeneratedMedia.model_validate(receipt | {"request_id": uuid4()}).validate_for(request, maximum_bytes=2048)
    with pytest.raises(ValueError):
        GeneratedMedia.model_validate(receipt | {"content_type": "image/png"}).validate_for(request, maximum_bytes=2048)
    with pytest.raises(ValueError):
        GeneratedMedia.model_validate(receipt).validate_for(request, maximum_bytes=1024)


def test_participant_media_requires_explicit_audience():
    with pytest.raises(ValidationError):
        AssetScope(policy="participants")
    with pytest.raises(ValidationError):
        AssetScope(policy="public", subject_ids=(uuid4(),))


@pytest.mark.asyncio
async def test_host_dispatch_applies_manifest_limits():
    calls = []

    class Plugin:
        async def generate(self, request):
            calls.append(request)
            return GeneratedMedia(request_id=request.request_id, content_hash="a" * 64,
                                  content_type="audio/wav", byte_size=2048)

    registry = MediaRegistry()
    registry.register(MediaManifest(id="voice", version="1", capabilities={"speech"},
                                    maximum_asset_bytes=1024), Plugin())
    request = MediaRequest(request_id=uuid4(), world_id=uuid4(), branch_id=uuid4(),
                           source_event_ids=(uuid4(),), kind="speech", authorized_description="Hello",
                           visibility=AssetScope(policy="author_only"), idempotency_key="voice")
    with pytest.raises(ValueError, match="does not support"):
        await registry.generate("voice", request.model_copy(update={"kind": "image"}))
    assert calls == []
    with pytest.raises(ValueError, match="size policy"):
        await registry.generate("voice", request)
    assert calls == [request]


@pytest.mark.asyncio
async def test_registered_artifact_transport_returns_bounded_bytes_for_exact_plan():
    data = b"RIFF\x04\x00\x00\x00WAVE"
    plugin_calls = []
    transport_calls = []

    class Plugin:
        async def generate(self, request):
            plugin_calls.append(request)
            return GeneratedMedia(
                request_id=request.request_id,
                content_hash=sha256(data).hexdigest(),
                content_type="audio/wav",
                byte_size=len(data),
            )

    class Transport:
        async def fetch(self, request, receipt, *, maximum_bytes):
            transport_calls.append((request, receipt, maximum_bytes))
            return data

    registry = MediaRegistry()
    registry.register(
        MediaManifest(
            id="trade-voice-bytes",
            version="3",
            capabilities={"speech"},
            subscriptions=[MediaSubscription(event_type="core.market_trade_executed.v1", kind="speech")],
            maximum_asset_bytes=len(data),
        ),
        Plugin(),
        artifact_transport=Transport(),
    )
    recipient_id = uuid4()
    source = AuthorizedMediaSource(
        event=_event_record(),
        recipient_id=recipient_id,
        authorized_description="A concise host-approved trade narration.",
        visibility=AssetScope(policy="public"),
    )
    plan = registry.plan_requests(source)[0]

    artifact = await registry.generate_artifact(plan)

    assert artifact.data == data
    assert artifact.receipt.content_hash == sha256(data).hexdigest()
    assert plugin_calls == [plan.request]
    assert transport_calls[0][0] == plan.request
    assert transport_calls[0][2] == len(data)


@pytest.mark.asyncio
async def test_artifact_transport_rejects_oversize_or_hash_mismatch():
    data = b"RIFF\x04\x00\x00\x00WAVE"

    class Plugin:
        async def generate(self, request):
            return GeneratedMedia(
                request_id=request.request_id,
                content_hash="a" * 64,
                content_type="audio/wav",
                byte_size=len(data),
            )

    class Transport:
        async def fetch(self, request, receipt, *, maximum_bytes):
            return data

    registry = MediaRegistry()
    registry.register(
        MediaManifest(
            id="bad-trade-voice",
            version="1",
            capabilities={"speech"},
            subscriptions=[MediaSubscription(event_type="core.market_trade_executed.v1", kind="speech")],
            maximum_asset_bytes=len(data),
        ),
        Plugin(),
        artifact_transport=Transport(),
    )
    plan = registry.plan_requests(AuthorizedMediaSource(
        event=_event_record(),
        recipient_id=uuid4(),
        authorized_description="A host-approved narration.",
        visibility=AssetScope(policy="public"),
    ))[0]
    with pytest.raises(ValueError, match="receipt hash"):
        await registry.generate_artifact(plan)

    with pytest.raises(ValueError, match="idempotency key"):
        registry.validate_plan(plan.model_copy(update={
            "request": plan.request.model_copy(update={"idempotency_key": "changed"}),
        }))


def _event_record(*, policy="public", subject_ids=()):
    return EventRecord(
        event_id=uuid4(),
        world_id=uuid4(),
        branch_id=uuid4(),
        sequence=7,
        tick=120,
        type="core.market_trade_executed.v1",
        command_id=uuid4(),
        actor_id=uuid4(),
        ruleset_version="1.0.0",
        payload={"secret": "raw private payload must not reach media plugins"},
        visibility={"policy": policy, **({"subject_ids": [str(value) for value in subject_ids]} if subject_ids else {})},
        state_version_after=3,
        commit_boundary=True,
        committed_at=utc_now(),
    )


def test_subscriptions_are_closed_typed_and_queryable_without_wildcards():
    subscription = MediaSubscription(event_type="core.market_trade_executed.v1", kind="speech")
    manifest = MediaManifest(
        id="voice",
        version="1",
        capabilities={"speech"},
        subscriptions=[subscription],
        maximum_asset_bytes=4096,
    )
    registry = MediaRegistry()
    registry.register(manifest, object())

    assert registry.matching_subscriptions("core.market_trade_executed.v1") == [
        MediaSubscriptionMatch(plugin_id="voice", manifest_version="1", subscription=subscription)
    ]
    assert registry.matching_manifests("core.market_trade_executed.v1") == [manifest]
    assert registry.matching_subscriptions("core.market_trade_executed.v1", kind="image") == []
    assert registry.manifests() == [manifest]

    with pytest.raises(ValidationError, match="registered stable event type"):
        MediaSubscription(event_type="*", kind="speech")
    with pytest.raises(ValidationError, match="registered stable event type"):
        MediaSubscription(event_type="unregistered.event.v1", kind="speech")
    with pytest.raises(ValidationError, match="declared in capabilities"):
        MediaManifest(
            id="voice-image",
            version="1",
            capabilities={"speech"},
            subscriptions=[MediaSubscription(event_type="core.market_trade_executed.v1", kind="image")],
            maximum_asset_bytes=4096,
        )
    with pytest.raises(ValueError, match="registered stable event type"):
        registry.matching_subscriptions("unregistered.event.v1")


def test_media_planning_is_recipient_scoped_idempotent_and_never_forwards_raw_payload():
    calls = []

    class Plugin:
        async def generate(self, request):
            calls.append(request)
            raise AssertionError("planning must not invoke the plugin")

    registry = MediaRegistry()
    registry.register(
        MediaManifest(
            id="trade-voice",
            version="2",
            capabilities={"speech", "image"},
            subscriptions=[
                MediaSubscription(event_type="core.market_trade_executed.v1", kind="speech"),
                MediaSubscription(event_type="core.market_trade_executed.v1", kind="image"),
            ],
            maximum_asset_bytes=4096,
        ),
        Plugin(),
    )
    recipient_id = uuid4()
    event = _event_record()
    source = AuthorizedMediaSource(
        event=event,
        recipient_id=recipient_id,
        authorized_description="The merchant accepts the offered grain.",
        visibility=AssetScope(policy="public"),
        asset_refs=("a" * 64,),
        style="calm narration",
    )

    plans = registry.plan_requests(source)
    repeated = registry.plan_requests(source)
    assert len(plans) == 2
    assert [plan.subscription.kind for plan in plans] == ["image", "speech"]
    for plan, retry in zip(plans, repeated, strict=True):
        request = plan.request
        assert request == retry.request
        assert request.recipient_id == recipient_id
        assert request.world_id == event.world_id
        assert request.branch_id == event.branch_id
        assert request.source_event_ids == (event.event_id,)
        assert request.authorized_description == source.authorized_description
        assert request.visibility == source.visibility
        assert request.asset_refs == source.asset_refs
        assert "raw private payload" not in str(request.model_dump(mode="json"))
        assert "payload" not in request.model_dump(mode="json")
    assert len({plan.request.idempotency_key for plan in plans}) == 2
    assert calls == []
    assert event.payload["secret"] == "raw private payload must not reach media plugins"

    other_recipient = source.model_copy(update={"recipient_id": uuid4()})
    assert all(
        left.request.idempotency_key != right.request.idempotency_key
        for left, right in zip(plans, registry.plan_requests(other_recipient), strict=True)
    )


def test_media_source_cannot_widen_private_or_author_only_event_audience():
    first, second, outsider = uuid4(), uuid4(), uuid4()
    event = _event_record(policy="private", subject_ids=(first, second))

    with pytest.raises(ValidationError, match="participant audience"):
        AuthorizedMediaSource(
            event=event,
            recipient_id=first,
            authorized_description="A private exchange.",
            visibility=AssetScope(policy="public"),
        )
    with pytest.raises(ValidationError, match="participant audience"):
        AuthorizedMediaSource(
            event=event,
            recipient_id=outsider,
            authorized_description="A private exchange.",
            visibility=AssetScope(policy="participants", subject_ids=(outsider,)),
        )
    with pytest.raises(ValidationError, match="participant audience"):
        AuthorizedMediaSource(
            event=_event_record(policy="private"),
            recipient_id=first,
            authorized_description="An event with no explicit audience.",
            visibility=AssetScope(policy="participants", subject_ids=(first,)),
        )

    source = AuthorizedMediaSource(
        event=event,
        recipient_id=first,
        authorized_description="A private exchange.",
        visibility=AssetScope(policy="participants", subject_ids=(first,)),
    )
    assert source.visibility.policy == "participants"

    with pytest.raises(ValidationError, match="remain author-only"):
        AuthorizedMediaSource(
            event=_event_record(policy="author_only"),
            recipient_id=first,
            authorized_description="An author note.",
            visibility=AssetScope(policy="public"),
        )
    with pytest.raises(ValidationError, match="cannot authorize media"):
        AuthorizedMediaSource(
            event=_event_record(policy="participants_and_observers", subject_ids=(first,)),
            recipient_id=first,
            authorized_description="An unsupported audience policy.",
            visibility=AssetScope(policy="participants", subject_ids=(first,)),
        )
