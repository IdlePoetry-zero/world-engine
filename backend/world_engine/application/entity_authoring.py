"""Previewed, replayable authoring for places and organizations."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal, localcontext
import json
from typing import Any
from uuid import UUID, uuid4, uuid5

from world_engine.domain.components import (
    GovernanceComponent,
    PlaceComponent,
    PlaceRoute,
    RenewableHarvestPolicy,
    RenewableHarvestRecipient,
    TreasuryComponent,
)
from world_engine.domain.errors import Conflict, InvalidEvent, NotFound, StaleVersion
from world_engine.domain.environmental_source_events import EnvironmentalSourceAccountOpenedEvent
from world_engine.domain.events import parse_event
from world_engine.domain.gameplay_events import (
    AuthorComponentRevisedEvent,
    ResourceAccountOpenedEvent,
)
from world_engine.domain.models import (
    CommandEnvelope,
    EntityCreatedEvent,
    MembershipRole,
    WorldEntity,
)
from world_engine.domain.reducers import apply_event, author_value_hash


_AUTHOR_ROLES = {MembershipRole.OWNER, MembershipRole.EDITOR}
_PREVIEW_LIFETIME = timedelta(hours=1)
_AUTHOR_EVENT_REFERENCE = {
    "kind": "rule",
    "reference_id": "core.author_entity_creation.v1",
    "notes": "Authenticated author creation from the reviewed, version-pinned draft.",
}


class EntityAuthoringApplication:
    """Create branch-local place and organization entities through preview/commit."""

    def __init__(self, repository: Any, operations: Any):
        self.repository = repository
        self.operations = operations

    def preview_place(self, scope, world_id, branch_id, expected_version, draft):
        self.repository.authorize_role(scope, world_id, _AUTHOR_ROLES)
        view = self.repository.read_world(scope, world_id, branch_id)
        self._require_version(view.branch.state_version, expected_version)
        preview_id = uuid4()
        entity_id = uuid5(preview_id, "place")
        place, source_events = self._place_components(view.state, preview_id, entity_id, draft)

        components: dict[str, object] = {}
        if draft.get("description"):
            components["public_description"] = draft["description"]
        if not source_events:
            components["place"] = place.model_dump(mode="json")
        entity = WorldEntity(
            id=entity_id,
            kind="place",
            name=draft["name"],
            status="active",
            components=components,
        )
        creation = EntityCreatedEvent(event_id=uuid5(preview_id, "entity-created"), entity=entity)
        events = [creation, *source_events]
        if source_events:
            events.append(self._initial_component_revision(
                preview_id=preview_id,
                entity_id=entity_id,
                author_id=scope.user_id,
                component_name="place",
                replacement=place.model_dump(mode="json"),
                revision_note="Attach the explicitly authored renewable harvest policies.",
            ))
        return self._save_preview(
            scope=scope,
            world_id=world_id,
            branch_id=view.branch.branch_id,
            expected_version=expected_version,
            preview_id=preview_id,
            creation_kind="place",
            entity_id=entity_id,
            draft=draft,
            events=events,
            created_accounts=[self._public_account_event(event) for event in source_events],
            state=view.state,
        )

    def commit_place(self, scope, world_id, preview_id):
        return self._commit(scope, world_id, preview_id, "place")

    def preview_organization(self, scope, world_id, branch_id, expected_version, draft):
        self.repository.authorize_role(scope, world_id, _AUTHOR_ROLES)
        view = self.repository.read_world(scope, world_id, branch_id)
        self._require_version(view.branch.state_version, expected_version)
        self._validate_existing_resource_ids(view.state, draft.get("treasury_resources", []))

        preview_id = uuid4()
        entity_id = uuid5(preview_id, "organization")
        components: dict[str, object] = {}
        if draft.get("description"):
            components["public_description"] = draft["description"]
        if draft.get("public_role"):
            components["public_role"] = draft["public_role"]
        governance = draft.get("governance")
        if governance is not None:
            governance_component = GovernanceComponent.model_validate(governance)
            self._validate_governance_references(view.state, governance_component)
            components["governance"] = governance_component.model_dump(mode="json")

        entity = WorldEntity(
            id=entity_id,
            kind=draft["kind"],
            name=draft["name"],
            location_id=UUID(draft["location_id"]) if draft.get("location_id") else None,
            status="active",
            components=components,
        )
        creation = EntityCreatedEvent(event_id=uuid5(preview_id, "entity-created"), entity=entity)
        events = [creation]
        account_events: list[ResourceAccountOpenedEvent] = []
        account_map: dict[str, UUID] = {}
        for resource_id in sorted(set(draft.get("treasury_resources", []))):
            account_id = uuid5(preview_id, f"organization-treasury:{resource_id}")
            account_event = ResourceAccountOpenedEvent(
                event_id=uuid5(preview_id, f"organization-treasury-open:{resource_id}"),
                account_id=account_id,
                owner_id=entity_id,
                resource_id=resource_id,
            )
            account_events.append(account_event)
            account_map[resource_id] = account_id
        events.extend(account_events)
        if account_map:
            treasury = TreasuryComponent(accounts=account_map)
            events.append(self._initial_component_revision(
                preview_id=preview_id,
                entity_id=entity_id,
                author_id=scope.user_id,
                component_name="treasury",
                replacement=treasury.model_dump(mode="json"),
                revision_note="Attach the explicitly requested zero-balance treasury accounts.",
            ))
        return self._save_preview(
            scope=scope,
            world_id=world_id,
            branch_id=view.branch.branch_id,
            expected_version=expected_version,
            preview_id=preview_id,
            creation_kind="organization",
            entity_id=entity_id,
            draft=draft,
            events=events,
            created_accounts=[self._public_account_event(event) for event in account_events],
            state=view.state,
        )

    def commit_organization(self, scope, world_id, preview_id):
        return self._commit(scope, world_id, preview_id, "organization")

    def _save_preview(
        self,
        *,
        scope,
        world_id: UUID,
        branch_id: UUID,
        expected_version: int | str,
        preview_id: UUID,
        creation_kind: str,
        entity_id: UUID,
        draft: dict[str, Any],
        events: list[object],
        created_accounts: list[dict[str, str]],
        state,
    ) -> dict[str, object]:
        projected = state
        for event in events:
            projected = apply_event(projected, event)

        snapshot = self.repository.snapshot(scope, world_id, branch_id)
        self._require_version(snapshot.state_version, expected_version)
        expires_at = (datetime.now(timezone.utc) + _PREVIEW_LIFETIME).isoformat()
        checkpoint_id = str(snapshot.snapshot_id)
        document = {
            "preview_id": str(preview_id),
            "world_id": str(world_id),
            "branch_id": str(branch_id),
            "creation_kind": creation_kind,
            "entity_id": str(entity_id),
            "expected_version": str(expected_version),
            "checkpoint_id": checkpoint_id,
            "checkpoint_sequence": str(snapshot.last_seq),
            "expires_at": expires_at,
            "events": [event.model_dump(mode="json") for event in events],
            "draft": draft,
            "entity": projected.entities[entity_id].model_dump(mode="json"),
            "created_accounts": created_accounts,
            "world_time_effect": False,
        }
        self._store_preview(scope, world_id, preview_id, document)
        return self._public_preview(document)

    def _commit(self, scope, world_id, preview_id: UUID, expected_kind: str) -> dict[str, object]:
        self.repository.authorize_role(scope, world_id, _AUTHOR_ROLES)
        document = self._load_preview(scope, world_id, preview_id)
        if (
            document.get("creation_kind") != expected_kind
            or document.get("preview_id") != str(preview_id)
            or document.get("world_id") != str(world_id)
            or not isinstance(document.get("events"), list)
        ):
            raise NotFound("Author entity preview not found")
        try:
            branch_id = UUID(document["branch_id"])
            entity_id = UUID(document["entity_id"])
            expected_version = int(document["expected_version"])
        except (KeyError, TypeError, ValueError) as exc:
            raise NotFound("Author entity preview not found") from exc
        key = f"author:{preview_id}"
        result = self.repository.read_command_receipt(
            scope, world_id, branch_id, preview_id, scope.user_id, key,
        )
        if result is None:
            if datetime.now(timezone.utc) > datetime.fromisoformat(document["expires_at"]):
                raise Conflict("This author entity preview has expired; review a fresh preview")
            command = CommandEnvelope(
                command_id=preview_id,
                actor_id=scope.user_id,
                idempotency_key=key,
                expected_state_version=expected_version,
                command_type=f"author.create_{expected_kind}",
                payload={
                    "preview_id": str(preview_id),
                    "checkpoint_id": document["checkpoint_id"],
                    "creation_kind": expected_kind,
                    "entity_id": str(entity_id),
                },
            )
            result = self.repository.commit_command(
                scope,
                world_id,
                branch_id,
                command,
                [parse_event(event) for event in document["events"]],
            )
            duplicate = result.duplicate
        else:
            duplicate = True
        return {
            "entity_id": str(entity_id),
            "command_id": str(result.command_id),
            "state_version": str(result.state_version),
            "duplicate": duplicate,
            "created_accounts": document.get("created_accounts", []),
            "world_time_effect": False,
        }

    @staticmethod
    def _place_components(state, preview_id: UUID, entity_id: UUID, draft: dict[str, Any]):
        EntityAuthoringApplication._validate_place_references(state, draft)
        EntityAuthoringApplication._validate_existing_resource_ids(
            state,
            [item["resource_id"] for item in draft.get("renewable_harvests", [])],
        )
        routes = [
            PlaceRoute(
                route_id=uuid5(preview_id, f"route:{index}"),
                from_place_id=entity_id,
                to_place_id=UUID(route["to_place_id"]),
                travel_ticks=route["travel_ticks"],
                capacity=route.get("capacity"),
                risk=route["risk"],
                access_tags=route.get("access_tags", []),
                travel_modes=route.get("travel_modes", []),
                is_open=route["is_open"],
            )
            for index, route in enumerate(draft.get("routes", []))
        ]
        source_events: list[EnvironmentalSourceAccountOpenedEvent] = []
        harvests: list[RenewableHarvestPolicy] = []
        for harvest in draft.get("renewable_harvests", []):
            resource_id = harvest["resource_id"]
            recipients = [
                RenewableHarvestRecipient(
                    entity_id=UUID(row["entity_id"]),
                    account_id=UUID(row["account_id"]),
                    per_period=row["per_period"],
                )
                for row in sorted(harvest["recipients"], key=lambda row: str(row["entity_id"]))
            ]
            with localcontext() as context:
                context.prec = 80
                per_period = sum((recipient.per_period for recipient in recipients), Decimal("0"))
            source_account_id = uuid5(preview_id, f"environmental-source:{harvest['policy_id']}")
            policy = RenewableHarvestPolicy(
                policy_id=harvest["policy_id"],
                resource_id=resource_id,
                source_account_id=source_account_id,
                period_ticks=harvest["period_ticks"],
                per_period=per_period,
                maximum_total=harvest["maximum_total"],
                issued_total=Decimal("0"),
                last_issued_period=0,
                recipients=recipients,
            )
            harvests.append(policy)
            source_events.append(EnvironmentalSourceAccountOpenedEvent(
                event_id=uuid5(preview_id, f"environmental-source-open:{harvest['policy_id']}"),
                account_id=source_account_id,
                owner_id=entity_id,
                resource_id=resource_id,
            ))
        place = PlaceComponent(
            place_type=draft["place_type"],
            region_id=UUID(draft["region_id"]) if draft.get("region_id") else None,
            parent_id=UUID(draft["parent_id"]) if draft.get("parent_id") else None,
            capacity=draft.get("capacity"),
            access_policy=draft["access_policy"],
            terrain=draft.get("terrain"),
            environment=draft.get("environment", {}),
            routes=routes,
            renewable_harvests=harvests,
        )
        return place, source_events

    @staticmethod
    def _validate_place_references(state, draft: dict[str, Any]) -> None:
        if draft.get("renewable_harvests") and draft["place_type"] != "region":
            raise InvalidEvent("Renewable harvest policies can only be authored on a region.")
        for key in ("region_id", "parent_id"):
            value = draft.get(key)
            if not value:
                continue
            entity = state.entities.get(UUID(value))
            if entity is None or entity.kind not in {"place", "location"}:
                raise InvalidEvent(f"{key} must reference an existing place.")
            if entity.status in {"dead", "retired"}:
                raise InvalidEvent(f"{key} cannot reference a retired place.")
            if key == "region_id":
                try:
                    existing_component = PlaceComponent.model_validate(entity.components.get("place"))
                except Exception as exc:
                    raise InvalidEvent("region_id must reference a place with typed regional data.") from exc
                if existing_component.place_type != "region":
                    raise InvalidEvent("region_id must reference a registered region.")
        for route in draft.get("routes", []):
            target = state.entities.get(UUID(route["to_place_id"]))
            if target is None or target.kind not in {"place", "location"}:
                raise InvalidEvent("Every route destination must be an existing place.")
            if target.status in {"dead", "retired"}:
                raise InvalidEvent("A route destination cannot be a retired place.")

    @staticmethod
    def _validate_governance_references(state, governance: GovernanceComponent) -> None:
        for member_id in governance.members:
            member = state.entities.get(member_id)
            if member is None or member.kind not in {"character", "person"}:
                raise InvalidEvent(f"Organization member {member_id} must reference an existing character.")
            if member.status in {"dead", "retired"}:
                raise InvalidEvent(f"Organization member {member_id} must be an active character.")
        active_references = [
            (governance.leader_id, "leader"),
            *((office.holder_id, "office holder") for office in governance.offices),
        ]
        for entity_id, label in active_references:
            if entity_id is None:
                continue
            entity = state.entities.get(entity_id)
            if entity is None or entity.kind not in {"character", "person"}:
                raise InvalidEvent(f"Organization {label} {entity_id} must reference an existing character.")
            if entity.status in {"dead", "retired"}:
                raise InvalidEvent(f"Organization {label} {entity_id} must reference an active character.")

    @staticmethod
    def _validate_existing_resource_ids(state, resource_ids: list[str]) -> None:
        valid = {account.resource_id for account in state.resources.values()}
        catalog = state.metadata.get("resource_catalog")
        if isinstance(catalog, dict):
            valid.update(key for key in catalog if isinstance(key, str))
        unknown = sorted(set(resource_ids) - valid)
        if unknown:
            raise InvalidEvent(f"Resource IDs must be defined in this world: {', '.join(unknown)}.")

    @staticmethod
    def _initial_component_revision(
        *,
        preview_id: UUID,
        entity_id: UUID,
        author_id: UUID,
        component_name: str,
        replacement: object,
        revision_note: str,
    ) -> AuthorComponentRevisedEvent:
        return AuthorComponentRevisedEvent(
            event_id=uuid5(preview_id, f"initialize-component:{component_name}"),
            entity_id=entity_id,
            author_id=author_id,
            component_name=component_name,
            expected_previous_hash=author_value_hash(None, component_name=component_name),
            replacement=replacement,
            revision_note=revision_note,
            provenance=[_AUTHOR_EVENT_REFERENCE],
        )

    @staticmethod
    def _public_account_event(event: object) -> dict[str, str]:
        if isinstance(event, EnvironmentalSourceAccountOpenedEvent):
            return {
                "account_id": str(event.account_id),
                "resource_id": event.resource_id,
                "owner_id": str(event.owner_id),
                "account_type": "environmental_source",
                "balance": "0",
                "credit_limit": "0",
            }
        if isinstance(event, ResourceAccountOpenedEvent):
            return {
                "account_id": str(event.account_id),
                "resource_id": event.resource_id,
                "owner_id": str(event.owner_id),
                "account_type": "holder",
                "balance": "0",
            }
        raise TypeError("unsupported account-open event")

    def _store_preview(self, scope, world_id: UUID, preview_id: UUID, document: dict[str, object]) -> None:
        with self.operations.connect() as db:
            db.execute(
                "INSERT INTO author_previews VALUES(?,?,?,?,?)",
                (*self.operations.identity(scope), str(preview_id), str(world_id), json.dumps(document, ensure_ascii=False)),
            )

    def _load_preview(self, scope, world_id: UUID, preview_id: UUID) -> dict[str, object]:
        with self.operations.connect() as db:
            row = db.execute(
                "SELECT document FROM author_previews WHERE tenant=? AND principal=? AND id=? AND world_id=?",
                (*self.operations.identity(scope), str(preview_id), str(world_id)),
            ).fetchone()
        if row is None:
            raise NotFound("Author entity preview not found")
        return json.loads(row[0])

    @staticmethod
    def _public_preview(document: dict[str, object]) -> dict[str, object]:
        return {
            key: document[key]
            for key in (
                "preview_id",
                "entity_id",
                "creation_kind",
                "expected_version",
                "expires_at",
                "checkpoint_sequence",
                "draft",
                "entity",
                "created_accounts",
                "world_time_effect",
            )
        }

    @staticmethod
    def _require_version(actual: int, expected: int | str) -> None:
        expected_int = int(expected)
        if actual != expected_int:
            raise StaleVersion(expected_int, actual)
