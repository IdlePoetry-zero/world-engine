"""Idempotent author-side operations for private kinship facts."""

from __future__ import annotations

from uuid import UUID, uuid5

from world_engine.domain.errors import Conflict, InvalidEvent, StaleVersion
from world_engine.domain.family_events import (
    KinshipLinkEndedEvent,
    KinshipLinkRecordedEvent,
    kinship_event_id,
)
from world_engine.domain.family_kinship import EventKinshipSource, KinshipLink, KinshipVisibility
from world_engine.domain.models import AccessScope, CommandEnvelope, MembershipRole


_AUTHOR_ROLES = {MembershipRole.OWNER, MembershipRole.EDITOR}


class FamilyAuthoringApplication:
    """Commit branch-versioned kinship edits through protected repository commands."""

    def __init__(self, repository):
        self.repository = repository

    def record_link(
        self,
        scope: AccessScope,
        world_id: UUID,
        branch_id: UUID,
        expected_state_version: int,
        *,
        parent_id: UUID,
        child_id: UUID,
        relationship: str,
        idempotency_key: str,
    ) -> dict:
        self._authorize(scope, world_id)
        command_id = _command_id(world_id, branch_id, idempotency_key)
        prior = self.repository.read_command_receipt(
            scope, world_id, branch_id, command_id, scope.user_id, idempotency_key,
        )
        if prior is not None:
            event = self._matching_prior(
                prior,
                expected_state_version,
                event_type="core.kinship_link_recorded.v1",
                fields={
                    "parent_id": str(parent_id),
                    "child_id": str(child_id),
                    "relationship": relationship,
                },
                nested_key="link",
                event_fields={"parent_id", "child_id", "relationship"},
            )
            return self._result(
                prior.model_copy(update={"duplicate": True}), event.event_id,
                link_id=UUID(event.payload["link"]["link_id"]),
            )

        view = self.repository.read_world(scope, world_id, branch_id)
        self._require_expected_version(expected_state_version, view.branch.state_version)
        ruleset_version = view.world.ruleset_version
        event_id = kinship_event_id(command_id, ruleset_version, "link_recorded")
        link_id = uuid5(command_id, f"{ruleset_version}:family_kinship.link")
        link = KinshipLink(
            link_id=link_id,
            parent_id=parent_id,
            child_id=child_id,
            relationship=relationship,
            created_tick=view.state.current_tick,
            source=EventKinshipSource(kind="event", event_id=event_id),
            visibility=KinshipVisibility(policy="participants"),
        )
        event = KinshipLinkRecordedEvent(
            event_id=event_id,
            world_id=world_id,
            branch_id=branch_id,
            command_id=command_id,
            ruleset_version=ruleset_version,
            actor_id=scope.user_id,
            effective_tick=view.state.current_tick,
            link=link,
        )
        payload = {
            "parent_id": str(parent_id),
            "child_id": str(child_id),
            "relationship": relationship,
        }
        command = CommandEnvelope(
            command_id=command_id,
            actor_id=scope.user_id,
            idempotency_key=idempotency_key,
            expected_state_version=expected_state_version,
            command_type="author.record_kinship_link",
            payload=payload,
        )
        committed = self.repository.commit_command(scope, world_id, branch_id, command, [event])
        return self._result(committed, event.event_id, link_id=link_id)

    def end_link(
        self,
        scope: AccessScope,
        world_id: UUID,
        branch_id: UUID,
        expected_state_version: int,
        *,
        link_id: UUID,
        expected_created_tick: int,
        expected_relationship: str | None,
        idempotency_key: str,
    ) -> dict:
        self._authorize(scope, world_id)
        command_id = _command_id(world_id, branch_id, idempotency_key)
        prior = self.repository.read_command_receipt(
            scope, world_id, branch_id, command_id, scope.user_id, idempotency_key,
        )
        if prior is not None:
            fields = {
                "link_id": str(link_id),
                "expected_created_tick": expected_created_tick,
                "expected_relationship": expected_relationship,
            }
            event = self._matching_prior(
                prior,
                expected_state_version,
                event_type="core.kinship_link_ended.v1",
                fields=fields,
                nested_key=None,
                event_fields=set(fields),
            )
            return self._result(prior.model_copy(update={"duplicate": True}), event.event_id, link_id=link_id)

        view = self.repository.read_world(scope, world_id, branch_id)
        self._require_expected_version(expected_state_version, view.branch.state_version)
        ruleset_version = view.world.ruleset_version
        event = KinshipLinkEndedEvent(
            event_id=kinship_event_id(command_id, ruleset_version, "link_ended"),
            world_id=world_id,
            branch_id=branch_id,
            command_id=command_id,
            ruleset_version=ruleset_version,
            actor_id=scope.user_id,
            effective_tick=view.state.current_tick,
            link_id=link_id,
            expected_created_tick=expected_created_tick,
            expected_relationship=expected_relationship,
        )
        payload = {
            "link_id": str(link_id),
            "expected_created_tick": expected_created_tick,
            "expected_relationship": expected_relationship,
        }
        command = CommandEnvelope(
            command_id=command_id,
            actor_id=scope.user_id,
            idempotency_key=idempotency_key,
            expected_state_version=expected_state_version,
            command_type="author.end_kinship_link",
            payload=payload,
        )
        committed = self.repository.commit_command(scope, world_id, branch_id, command, [event])
        return self._result(committed, event.event_id, link_id=link_id)

    def _authorize(self, scope: AccessScope, world_id: UUID) -> None:
        self.repository.authorize_role(scope, world_id, _AUTHOR_ROLES)

    @staticmethod
    def _require_expected_version(expected: int, actual: int) -> None:
        if expected != actual:
            raise StaleVersion(expected, actual)

    @staticmethod
    def _matching_prior(
        prior,
        expected_state_version: int,
        *,
        event_type: str,
        fields: dict[str, object],
        nested_key: str | None,
        event_fields: set[str],
    ):
        if expected_state_version != prior.state_version - 1 or len(prior.events) != 1:
            raise Conflict("Idempotency key was reused for a different kinship request.")
        event = prior.events[0]
        if event.type != event_type:
            raise Conflict("Idempotency key was reused for a different kinship operation.")
        payload = event.payload
        observed = payload.get(nested_key) if nested_key else payload
        if not isinstance(observed, dict) or any(observed.get(key) != value for key, value in fields.items()):
            raise Conflict("Idempotency key was reused with different kinship details.")
        # Optional end preconditions are explicit in the command body. The
        # event may omit a defaulted null field in a compact historic payload.
        if nested_key is None and set(observed).intersection(event_fields) != event_fields:
            raise Conflict("Existing kinship command payload is incomplete.")
        return event

    @staticmethod
    def _result(committed, event_id: UUID, *, link_id: UUID) -> dict:
        return {
            "command_id": str(committed.command_id),
            "event_id": str(event_id),
            "link_id": str(link_id),
            "state_version": str(committed.state_version),
            "duplicate": bool(committed.duplicate),
            "world_time_effect": False,
        }


def _command_id(world_id: UUID, branch_id: UUID, idempotency_key: str) -> UUID:
    if not idempotency_key or not idempotency_key.strip() or len(idempotency_key) > 200:
        raise InvalidEvent("A nonempty idempotency key of at most 200 characters is required.")
    return uuid5(world_id, f"family_kinship.command:{branch_id}:{idempotency_key}")
