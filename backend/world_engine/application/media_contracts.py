"""Host-to-plugin contracts for generating assets from authorized world facts."""

from __future__ import annotations

from hashlib import sha256
from dataclasses import dataclass
from typing import Annotated, Literal, Protocol
from uuid import UUID, uuid5

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, StringConstraints, model_validator

from world_engine.domain.events import EVENT_TYPES
from world_engine.domain.models import EventRecord


AssetHash = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
MediaKind = Literal["speech", "image"]

_MEDIA_REQUEST_NAMESPACE = UUID("1aa8d717-2eaf-44d4-9ce3-83290439bb29")


def _registered_event_type(value: str) -> str:
    if value not in EVENT_TYPES:
        raise ValueError("Media subscriptions must name a registered stable event type")
    return value


StableEventType = Annotated[str, StringConstraints(min_length=1, max_length=160), AfterValidator(_registered_event_type)]


class MediaContract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class AssetScope(MediaContract):
    policy: Literal["public", "participants", "author_only"]
    subject_ids: tuple[UUID, ...] = Field(default=(), max_length=256)

    @model_validator(mode="after")
    def validate_audience(self):
        if (self.policy == "participants") != bool(self.subject_ids):
            raise ValueError("Only participant visibility accepts a nonempty explicit audience")
        if len(set(self.subject_ids)) != len(self.subject_ids):
            raise ValueError("Visibility subjects must be unique")
        return self


class MediaSubscription(MediaContract):
    """A narrow event type and output kind a media adapter opts into."""

    event_type: StableEventType
    kind: MediaKind


class AuthorizedMediaSource(MediaContract):
    """Host-filtered media input; the raw event payload is never forwarded."""

    event: EventRecord
    recipient_id: UUID
    authorized_description: str = Field(min_length=1, max_length=24_000)
    asset_refs: tuple[AssetHash, ...] = Field(default=(), max_length=32)
    style: str = Field(default="", max_length=1_000)
    visibility: AssetScope

    @model_validator(mode="after")
    def source_is_registered_and_audience_is_not_widened(self) -> AuthorizedMediaSource:
        if self.event.type not in EVENT_TYPES:
            raise ValueError("Media source must reference a registered stable event type")
        if self.visibility.policy == "participants" and self.recipient_id not in self.visibility.subject_ids:
            raise ValueError("The media recipient must belong to the requested participant audience")

        raw_policy = self.event.visibility.get("policy")
        raw_subjects_value = self.event.visibility.get("subject_ids", [])
        if not isinstance(raw_subjects_value, list) or any(
            not isinstance(value, str) for value in raw_subjects_value
        ):
            raise ValueError("Event visibility has no valid explicit subject audience")
        try:
            raw_subjects = {UUID(value) for value in raw_subjects_value}
        except ValueError as exc:
            raise ValueError("Event visibility has an invalid subject audience") from exc

        if raw_policy == "public":
            if self.visibility.policy not in {"public", "participants"}:
                raise ValueError("Public events may produce public or recipient-scoped media only")
        elif raw_policy == "author_only":
            if self.visibility.policy != "author_only":
                raise ValueError("Author-only events must remain author-only in media outputs")
        elif raw_policy in {"private", "subject_ids"}:
            if (
                not raw_subjects
                or self.visibility.policy != "participants"
                or not set(self.visibility.subject_ids).issubset(raw_subjects)
            ):
                raise ValueError("Private event media requires a participant audience within event subject_ids")
        else:
            raise ValueError("Event visibility policy cannot authorize media output")
        return self


class MediaRequest(MediaContract):
    schema_version: Literal["1.0"] = "1.0"
    request_id: UUID
    world_id: UUID
    branch_id: UUID
    source_event_ids: tuple[UUID, ...] = Field(min_length=1, max_length=100)
    # Optional for compatibility with existing direct callers. The registry's
    # event planner always sets it, keeping every planned request recipient-bound.
    recipient_id: UUID | None = None
    kind: MediaKind
    authorized_description: str = Field(min_length=1, max_length=24000)
    asset_refs: tuple[AssetHash, ...] = Field(default=(), max_length=32)
    style: str = Field(default="", max_length=1000)
    visibility: AssetScope
    idempotency_key: str = Field(min_length=1, max_length=200)


class GeneratedMedia(MediaContract):
    schema_version: Literal["1.0"] = "1.0"
    request_id: UUID
    content_hash: AssetHash
    content_type: Literal["audio/wav", "audio/mpeg", "audio/ogg", "image/png", "image/jpeg", "image/webp"]
    byte_size: int = Field(gt=0, le=64 * 1024 * 1024)

    def validate_for(self, request: MediaRequest, *, maximum_bytes: int) -> None:
        expected_type = "audio/" if request.kind == "speech" else "image/"
        if self.request_id != request.request_id:
            raise ValueError("Asset receipt belongs to a different request")
        if not self.content_type.startswith(expected_type) or self.byte_size > maximum_bytes:
            raise ValueError("Asset receipt does not match the requested media kind or size policy")


class MediaPlugin(Protocol):
    async def generate(self, request: MediaRequest) -> GeneratedMedia:
        """Return an asset receipt. The host retains source and audience metadata."""


class MediaArtifactTransport(Protocol):
    async def fetch(
        self,
        request: MediaRequest,
        receipt: GeneratedMedia,
        *,
        maximum_bytes: int,
    ) -> bytes:
        """Return bounded asset bytes without returning a path or URL."""


@dataclass(frozen=True, slots=True)
class GeneratedMediaArtifact:
    """A validated receipt and its in-memory bytes; never persisted in job JSON."""

    receipt: GeneratedMedia
    data: bytes


class MediaManifest(MediaContract):
    id: Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9_.-]{0,79}$")]
    version: str = Field(min_length=1, max_length=40)
    protocol: Literal["1.0"] = "1.0"
    capabilities: frozenset[MediaKind] = Field(min_length=1)
    subscriptions: tuple[MediaSubscription, ...] = Field(default=(), max_length=1024)
    maximum_asset_bytes: int = Field(gt=0, le=64 * 1024 * 1024)

    @model_validator(mode="after")
    def subscriptions_match_capabilities(self) -> MediaManifest:
        subscriptions = [(item.event_type, item.kind) for item in self.subscriptions]
        if len(subscriptions) != len(set(subscriptions)):
            raise ValueError("Media subscriptions must be unique by event type and kind")
        if any(item.kind not in self.capabilities for item in self.subscriptions):
            raise ValueError("Every media subscription kind must be declared in capabilities")
        return self


class MediaSubscriptionMatch(MediaContract):
    plugin_id: str
    manifest_version: str
    subscription: MediaSubscription


class PlannedMediaRequest(MediaContract):
    """An authorized request plan. It is not a job and is never auto-dispatched."""

    plugin_id: str
    manifest_version: str
    subscription: MediaSubscription
    request: MediaRequest


class MediaRegistry:
    """Explicit host registration. Manifests from world data cannot load code."""

    def __init__(self):
        self._plugins: dict[
            str, tuple[MediaManifest, MediaPlugin, MediaArtifactTransport | None]
        ] = {}

    def register(
        self,
        manifest: MediaManifest,
        plugin: MediaPlugin,
        *,
        artifact_transport: MediaArtifactTransport | None = None,
    ) -> None:
        if manifest.id in self._plugins:
            raise ValueError("Media plugin ID is already registered")
        self._plugins[manifest.id] = manifest, plugin, artifact_transport

    def manifests(self) -> list[MediaManifest]:
        return [entry[0] for _, entry in sorted(self._plugins.items())]

    def matching_subscriptions(
        self, event_type: str, *, kind: MediaKind | None = None
    ) -> list[MediaSubscriptionMatch]:
        """Query registered subscriptions for one stable event type."""
        if event_type not in EVENT_TYPES:
            raise ValueError("Media subscription queries must use a registered stable event type")
        matches = [
            MediaSubscriptionMatch(
                plugin_id=manifest.id,
                manifest_version=manifest.version,
                subscription=subscription,
            )
            for manifest, _plugin, _transport in self._plugins.values()
            for subscription in manifest.subscriptions
            if subscription.event_type == event_type and (kind is None or subscription.kind == kind)
        ]
        return sorted(matches, key=lambda item: (item.plugin_id, item.subscription.kind))

    def matching_manifests(self, event_type: str, *, kind: MediaKind | None = None) -> list[MediaManifest]:
        """Return each opted-in manifest once, in stable plugin ID order."""
        plugin_ids = {
            item.plugin_id for item in self.matching_subscriptions(event_type, kind=kind)
        }
        return [self._plugins[plugin_id][0] for plugin_id in sorted(plugin_ids)]

    def plan_requests(self, source: AuthorizedMediaSource) -> list[PlannedMediaRequest]:
        """Build deterministic per-plugin requests from host-authorized text only."""
        plans: list[PlannedMediaRequest] = []
        for match in self.matching_subscriptions(source.event.type):
            idempotency_key = self._idempotency_key(
                plugin_id=match.plugin_id,
                manifest_version=match.manifest_version,
                source=source,
                kind=match.subscription.kind,
            )
            request = MediaRequest(
                request_id=uuid5(_MEDIA_REQUEST_NAMESPACE, idempotency_key),
                world_id=source.event.world_id,
                branch_id=source.event.branch_id,
                source_event_ids=(source.event.event_id,),
                recipient_id=source.recipient_id,
                kind=match.subscription.kind,
                authorized_description=source.authorized_description,
                asset_refs=source.asset_refs,
                style=source.style,
                visibility=source.visibility,
                idempotency_key=idempotency_key,
            )
            plans.append(
                PlannedMediaRequest(
                    plugin_id=match.plugin_id,
                    manifest_version=match.manifest_version,
                    subscription=match.subscription,
                    request=request,
                )
            )
        return plans

    def validate_plan(self, plan: PlannedMediaRequest) -> None:
        """Require a plan to match this exact registered manifest and subscription."""
        plan = PlannedMediaRequest.model_validate(plan.model_dump(mode="python"))
        entry = self._plugins.get(plan.plugin_id)
        if entry is None:
            raise LookupError("Media plugin is not registered")
        manifest, _plugin, _transport = entry
        if manifest.version != plan.manifest_version:
            raise ValueError("The planned media manifest version is no longer registered")
        if plan.subscription not in manifest.subscriptions:
            raise ValueError("The planned media subscription is not registered")
        request = plan.request
        if request.kind != plan.subscription.kind or request.kind not in manifest.capabilities:
            raise ValueError("The media request kind does not match the registered subscription")
        if request.recipient_id is None or len(request.source_event_ids) != 1:
            raise ValueError("A durable media plan requires one event and an explicit recipient")
        material = ":".join((
            plan.plugin_id,
            manifest.version,
            str(request.world_id),
            str(request.branch_id),
            str(request.source_event_ids[0]),
            str(request.recipient_id),
            request.kind,
        ))
        expected_key = f"media:v1:{sha256(material.encode('utf-8')).hexdigest()}"
        if request.idempotency_key != expected_key:
            raise ValueError("The media request idempotency key is not derived from its stable source")
        if request.request_id != uuid5(_MEDIA_REQUEST_NAMESPACE, expected_key):
            raise ValueError("The media request ID is not derived from its idempotency key")

    async def generate(self, plugin_id: str, request: MediaRequest) -> GeneratedMedia:
        """Dispatch a request assembled from authorized sources by the host."""
        entry = self._plugins.get(plugin_id)
        if entry is None:
            raise LookupError("Media plugin is not registered")
        manifest, plugin, _transport = entry
        if request.kind not in manifest.capabilities:
            raise ValueError("The media plugin does not support the requested kind")
        receipt = GeneratedMedia.model_validate(await plugin.generate(request))
        receipt.validate_for(request, maximum_bytes=manifest.maximum_asset_bytes)
        return receipt

    async def generate_artifact(self, plan: PlannedMediaRequest) -> GeneratedMediaArtifact:
        """Generate and retrieve bytes through the registered in-process transport."""
        self.validate_plan(plan)
        manifest, _plugin, transport = self._plugins[plan.plugin_id]
        if transport is None:
            raise LookupError("The media plugin has no registered artifact byte transport")
        receipt = await self.generate(plan.plugin_id, plan.request)
        data = await transport.fetch(
            plan.request,
            receipt,
            maximum_bytes=manifest.maximum_asset_bytes,
        )
        if not isinstance(data, bytes):
            raise ValueError("The registered artifact transport must return bytes")
        if len(data) > manifest.maximum_asset_bytes or len(data) != receipt.byte_size:
            raise ValueError("The media artifact bytes exceed or differ from the registered size")
        if sha256(data).hexdigest() != receipt.content_hash:
            raise ValueError("The media artifact bytes do not match the generated receipt hash")
        return GeneratedMediaArtifact(receipt=receipt, data=data)

    @staticmethod
    def _idempotency_key(
        *, plugin_id: str, manifest_version: str, source: AuthorizedMediaSource, kind: MediaKind
    ) -> str:
        material = ":".join((
            plugin_id,
            manifest_version,
            str(source.event.world_id),
            str(source.event.branch_id),
            str(source.event.event_id),
            str(source.recipient_id),
            kind,
        ))
        digest = sha256(material.encode("utf-8")).hexdigest()
        return f"media:v1:{digest}"
