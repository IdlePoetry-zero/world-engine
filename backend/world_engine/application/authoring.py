"""Explicit author revision previews and version-checked, replayable commits."""
from datetime import datetime, timedelta, timezone
import json
from uuid import UUID, uuid4, uuid5

from pydantic import ValidationError

from world_engine.domain.components import (
    COMPONENT_REGISTRY,
    GoalCondition,
    GoalRecord,
    KnowledgeRecord,
    PersonalityComponent,
    PersonalityLockViolation,
    PopulationSeatPolicy,
)
from world_engine.domain.errors import Conflict, InvalidEvent, NotFound, StaleVersion
from world_engine.domain.events import parse_event
from world_engine.domain.gameplay_events import AuthorComponentRevisedEvent, GoalCreatedEvent, KnowledgeAcquiredEvent
from world_engine.domain.models import CharacterCardSource, CommandEnvelope, EntityCreatedEvent, MembershipRole, WorldEntity
from world_engine.domain.npc_plan_review_events import NpcPlanReviewScheduledEvent, plan_authored_npc_review
from world_engine.domain.reducers import apply_event, author_value_hash
from world_engine.domain.world_books import WorldBookEntry, WorldBookImportedEvent

from .author_rule_checks import AuthorRuleConflict, author_component_rule_errors
from .world_book_import import load_world_book


def _destination_condition(state, actor_id, location_id, destination_id, description):
    destination = state.entities.get(destination_id)
    if not description.strip():
        raise InvalidEvent("A destination goal requires a description.")
    if destination is None or destination.kind not in {"place", "location"} or destination.status != "active":
        raise InvalidEvent("A destination goal requires an active place in this world.")
    if destination.id == location_id:
        raise InvalidEvent("A destination goal must differ from the starting location.")
    return GoalCondition(kind="reach_location", subject_id=actor_id, target_id=destination.id)


class AuthoringApplication:
    def __init__(self, repository, operations):
        self.repository, self.operations = repository, operations

    def preview(self, scope, world_id, branch_id, entity_id, component_name, replacement, reason):
        self.repository.authorize_role(scope, world_id, {MembershipRole.OWNER, MembershipRole.EDITOR})
        view = self.repository.read_world(scope, world_id, branch_id)
        entity = view.state.entities.get(entity_id)
        if entity is None:
            raise NotFound("Author target entity not found")
        preview_id = uuid4()
        current = entity.components.get(component_name)
        spec = COMPONENT_REGISTRY.get(component_name)
        if spec is None:
            raise InvalidEvent("The selected component is not registered")
        if (component_name == "capabilities" and entity_id == view.state.player_id
                and isinstance(replacement, dict) and replacement.get("combat_death_protection") is True):
            raise AuthorRuleConflict([{
                "path": "patch_proposal.replacement.combat_death_protection",
                "type": "capabilities.combat_death_protection", "message": "战斗保护只适用于 NPC。",
            }])
        if component_name == "population" and isinstance(replacement, dict):
            replacement = self._revise_population_seat_policy(current, replacement)
        if isinstance(replacement, dict) and spec.replaceable:
            revision = current.get("revision") if isinstance(current, dict) else None
            replacement = replacement | {"revision": (revision or 0) + 1}
        try:
            event = AuthorComponentRevisedEvent(entity_id=entity_id, author_id=scope.user_id,
                component_name=component_name,
                expected_previous_hash=author_value_hash(current, component_name=component_name),
                replacement=replacement,
                revision_note=reason, provenance=[{"kind": "rule", "reference_id": "core.author_revision.v1",
                    "notes": "Authenticated author revision; the command records its request and account."}])
        except ValueError as error:
            raise InvalidEvent("Author component revision is invalid") from error
        field_errors = author_component_rule_errors(
            view.state.metadata, view.state.entities, entity_id, entity.kind, component_name, event.replacement,
            current=current, path_prefix="patch_proposal.replacement",
        )
        if field_errors:
            raise AuthorRuleConflict(field_errors)
        try:
            after = apply_event(view.state, event)
        except InvalidEvent as error:
            if isinstance(error.__cause__, PersonalityLockViolation):
                raise AuthorRuleConflict([
                    {"path": f"patch_proposal.replacement.{field}", "type": "personality.locked_fields",
                     "message": "此设定字段已锁定。请先提交解锁，再修改该字段。"}
                    for field in error.__cause__.fields
                ]) from None
            if isinstance(error.__cause__, ValidationError):
                raise InvalidEvent("Author component revision failed world validation") from None
            raise
        snapshot = self.repository.snapshot(scope, world_id, view.branch.branch_id)
        if snapshot.state_version != view.branch.state_version:
            raise StaleVersion(view.branch.state_version, snapshot.state_version)
        document = {"preview_id": str(preview_id), "world_id": str(world_id), "branch_id": str(view.branch.branch_id),
            "expected_version": str(view.branch.state_version), "entity_id": str(entity_id), "component_name": component_name,
            "before": current, "after": after.entities[entity_id].components[component_name],
            "reason": reason, "checkpoint_id": str(snapshot.snapshot_id), "checkpoint_sequence": str(snapshot.last_seq),
            "expires_at": (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
            "event": event.model_dump(mode="json"), "world_time_effect": False}
        self._store_preview(scope, world_id, preview_id, document)
        return self._public(document)

    @staticmethod
    def _revise_population_seat_policy(current, replacement):
        proposed_value = replacement.get("seat_policy")
        if not isinstance(proposed_value, dict):
            return replacement

        # The nested revision belongs to the server. Ignore even an invalid
        # client value before parsing the policy's canonical fields.
        proposed_fields = dict(proposed_value)
        proposed_fields.pop("revision", None)
        previous_value = current.get("seat_policy") if isinstance(current, dict) else None
        try:
            proposed_policy = PopulationSeatPolicy.model_validate(proposed_fields)
            previous_policy = (
                PopulationSeatPolicy.model_validate(previous_value)
                if isinstance(previous_value, dict) else None
            )
        except ValueError as error:
            raise InvalidEvent("Author population seat policy is invalid") from error

        proposed_canonical = proposed_policy.model_dump(mode="json", exclude={"revision"})
        if previous_policy is None:
            revision = 1
        else:
            previous_canonical = previous_policy.model_dump(mode="json", exclude={"revision"})
            revision = (
                previous_policy.revision
                if proposed_canonical == previous_canonical
                else previous_policy.revision + 1
            )

        policy = proposed_policy.model_dump(mode="json") | {"revision": revision}
        return replacement | {"seat_policy": policy}

    def commit(self, scope, world_id, preview_id, expected_version):
        self.repository.authorize_role(scope, world_id, {MembershipRole.OWNER, MembershipRole.EDITOR})
        document = self._load_preview(scope, world_id, preview_id)
        if "event" not in document:
            raise NotFound("Author preview not found")
        if str(expected_version) != document["expected_version"]:
            raise Conflict("The reviewed version does not match this preview")
        if datetime.now(timezone.utc) > datetime.fromisoformat(document["expires_at"]):
            raise Conflict("This author preview has expired; review a fresh preview")
        command = CommandEnvelope(command_id=preview_id, actor_id=scope.user_id,
            idempotency_key=f"author:{preview_id}", expected_state_version=int(expected_version),
            command_type="author.revise_component", payload={"preview_id": str(preview_id),
                "reason": document["reason"], "checkpoint_id": document["checkpoint_id"]})
        result = self.repository.commit_command(scope, world_id, UUID(document["branch_id"]), command, [parse_event(document["event"])])
        return {"command_id": str(result.command_id), "state_version": str(result.state_version),
                "duplicate": result.duplicate, "checkpoint_id": document["checkpoint_id"], "world_time_effect": False}

    def preview_character(
        self, scope, world_id, branch_id, expected_version, draft,
        *, character_card_source: CharacterCardSource | None = None,
    ):
        self.repository.authorize_role(scope, world_id, {MembershipRole.OWNER, MembershipRole.EDITOR})
        view = self.repository.read_world(scope, world_id, branch_id)
        if view.branch.state_version != expected_version:
            raise StaleVersion(expected_version, view.branch.state_version)
        preview_id = uuid4()
        entity_id = uuid5(preview_id, "character")
        location_id = UUID(draft["location_id"])
        conditions = []
        if destination_id := draft.get("goal_destination_id"):
            conditions.append(_destination_condition(view.state, entity_id, location_id, UUID(destination_id), draft["goal"]))
        components = {
            "personality": PersonalityComponent(values=draft["values"], traits=draft["traits"]).model_dump(mode="json"),
            "health": {"current": 100, "maximum": 100},
            **({"capabilities": {"grants": ["world.travel"]}} if conditions else {}),
            **({"public_role": draft["public_role"]} if draft["public_role"] else {}),
            **({"public_description": draft["description"]} if draft["description"] else {}),
        }
        creation = EntityCreatedEvent(
            entity=WorldEntity(
                id=entity_id, kind="character", name=draft["name"], location_id=location_id,
                components=components,
            ),
            character_card_source=character_card_source,
        )
        events = [creation]
        if draft["secret"]:
            events.append(KnowledgeAcquiredEvent(record=KnowledgeRecord(
                id=uuid5(preview_id, "secret"), holder_id=entity_id, kind="belief", claim=draft["secret"],
                received_tick=view.state.current_tick, source_event_id=creation.event_id,
            )))
        if draft["goal"]:
            events.append(GoalCreatedEvent(goal=GoalRecord(
                id=uuid5(preview_id, "goal"), owner_id=entity_id, title=draft["goal"][:240],
                description=draft["goal"], created_tick=view.state.current_tick,
                conditions=conditions, auto_complete=bool(conditions),
            )))
        projected = view.state
        for event in events:
            projected = apply_event(projected, event)
        enrollment = None
        if count := draft.get("review_count", 0):
            self.repository.world_actor_control_ids(scope, world_id)
            enrollment = self._make_npc_review_event(
                projected, scope, world_id, branch_id, preview_id, entity_id,
                count, draft["review_interval_days"],
            )
            apply_event(projected, enrollment)
            events.append(enrollment)
        snapshot = self.repository.snapshot(scope, world_id, branch_id)
        if snapshot.state_version != expected_version:
            raise StaleVersion(expected_version, snapshot.state_version)
        public = {
            "preview_id": str(preview_id), "entity_id": str(entity_id), "expected_version": str(expected_version),
            "expires_at": (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
            "checkpoint_sequence": str(snapshot.last_seq), "draft": draft,
            **({"review_schedule": enrollment.schedule.model_dump(mode="json")} if enrollment else {}),
        }
        self._store_preview(scope, world_id, preview_id, public | {
            "branch_id": str(branch_id), "checkpoint_id": str(snapshot.snapshot_id),
            "events": [event.model_dump(mode="json") for event in events],
        })
        return public

    def commit_character(self, scope, world_id, preview_id):
        self.repository.authorize_role(scope, world_id, {MembershipRole.OWNER, MembershipRole.EDITOR})
        document = self._load_preview(scope, world_id, preview_id)
        if "events" not in document or document.get("kind", "character_creation") != "character_creation":
            raise NotFound("Character preview not found")
        result, duplicate = self._commit_author_event_batch(scope, world_id, preview_id, document, "author.create_character")
        return {"entity_id": document["entity_id"], "command_id": str(result.command_id),
                "state_version": str(result.state_version), "duplicate": duplicate}

    def preview_world_book(self, scope, world_id, branch_id, expected_version, title, book_json):
        self.repository.authorize_role(scope, world_id, {MembershipRole.OWNER, MembershipRole.EDITOR})
        view = self.repository.read_world(scope, world_id, branch_id)
        if view.branch.state_version != expected_version:
            raise StaleVersion(expected_version, view.branch.state_version)

        entries, skipped, source = load_world_book(book_json)
        if len(view.state.world_book_entries) + len(entries) > 512:
            raise InvalidEvent("The branch cannot contain more than 512 world-book entries.")
        snapshot = self.repository.snapshot(scope, world_id, branch_id)
        if snapshot.state_version != expected_version:
            raise StaleVersion(expected_version, snapshot.state_version)

        preview_id = uuid4()
        public = {
            "preview_id": str(preview_id), "title": title, "expected_version": str(expected_version),
            "entries": entries, "skipped_entries": skipped,
            "expires_at": (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
        }
        self._store_preview(scope, world_id, preview_id, public | {
            "kind": "world_book_import", "branch_id": str(branch_id),
            "checkpoint_id": str(snapshot.snapshot_id), "source": source,
        })
        return public

    def commit_world_book(self, scope, world_id, preview_id, public_keys):
        self.repository.authorize_role(scope, world_id, {MembershipRole.OWNER, MembershipRole.EDITOR})
        document = self._load_preview(scope, world_id, preview_id)
        if document.get("kind") != "world_book_import":
            raise NotFound("World-book preview not found")
        approved = set(public_keys)
        if len(approved) != len(public_keys) or not approved.issubset(
            {entry["source_key"] for entry in document["entries"]}
        ):
            raise InvalidEvent("Public world-book choices must match unique entries in the reviewed preview.")

        branch_id = UUID(document["branch_id"])
        key = f"author:{preview_id}"
        if datetime.now(timezone.utc) > datetime.fromisoformat(document["expires_at"]):
            if self.repository.read_command_receipt(scope, world_id, branch_id, preview_id, scope.user_id, key) is None:
                raise Conflict("This author preview has expired; review a fresh preview")

        event_id = uuid5(preview_id, "world-book")
        entries = [
            WorldBookEntry(
                **row, entry_id=uuid5(event_id, row["source_key"]),
                visibility="public" if row["source_key"] in approved else "author_only",
            )
            for row in document["entries"]
        ]
        event = WorldBookImportedEvent(
            event_id=event_id, title=document["title"], entries=tuple(entries), **document["source"],
        )
        command = CommandEnvelope(
            command_id=preview_id, actor_id=scope.user_id, idempotency_key=key,
            expected_state_version=int(document["expected_version"]), command_type="author.import_world_book",
            payload={"preview_id": str(preview_id), "checkpoint_id": document["checkpoint_id"],
                     "public_keys": sorted(approved)},
        )
        result = self.repository.commit_command(scope, world_id, branch_id, command, [event])
        return {"entry_count": len(entries), "state_version": str(result.state_version), "duplicate": result.duplicate}

    def preview_npc_review(self, scope, world_id, branch_id, expected_version, actor_id, count, interval_days,
                           *, goal_description="", goal_destination_id=None):
        if goal_description.strip() and goal_destination_id is None:
            raise InvalidEvent("A new destination goal requires a place.")
        controlled = self.repository.world_actor_control_ids(scope, world_id)
        if actor_id in controlled:
            raise Conflict("This character is controlled by an account.")
        view = self.repository.read_world(scope, world_id, branch_id)
        if view.branch.state_version != expected_version:
            raise StaleVersion(expected_version, view.branch.state_version)
        preview_id = uuid4()
        projected = view.state
        events = []
        if goal_destination_id is not None:
            actor = projected.entities.get(actor_id)
            if actor is None or actor.status != "active" or actor.kind not in {"person", "character"}:
                raise InvalidEvent("A destination goal requires an active character.")
            condition = _destination_condition(projected, actor_id, actor.location_id, goal_destination_id, goal_description)
            if any(goal.owner_id == actor_id and goal.status == "active" and any(
                item.kind == "reach_location" and item.subject_id == actor_id and item.target_id == goal_destination_id
                for item in goal.conditions
            ) for goal in projected.goals):
                raise Conflict("This character already has an active goal for that destination.")
            goal = GoalRecord(id=uuid5(preview_id, "goal"), owner_id=actor_id, title=goal_description[:240],
                              description=goal_description, created_tick=projected.current_tick,
                              updated_tick=projected.current_tick, conditions=[condition], auto_complete=True)
            created = GoalCreatedEvent(goal=goal)
            projected = apply_event(projected, created)
            events.append(created)
        event = self._make_npc_review_event(projected, scope, world_id, branch_id, preview_id, actor_id, count, interval_days)
        apply_event(projected, event)
        events.append(event)
        snapshot = self.repository.snapshot(scope, world_id, branch_id)
        if snapshot.state_version != expected_version:
            raise StaleVersion(expected_version, snapshot.state_version)
        public = {
            "preview_id": str(preview_id), "entity_id": str(actor_id), "expected_version": str(expected_version),
            "review_schedule": event.schedule.model_dump(mode="json"),
            "expires_at": (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
            **({"goal": goal.model_dump(mode="json")} if goal_destination_id is not None else {}),
        }
        self._store_preview(scope, world_id, preview_id, public | {
            "branch_id": str(branch_id), "checkpoint_id": str(snapshot.snapshot_id),
            "kind": "npc_review", "events": [item.model_dump(mode="json") for item in events],
        })
        return public

    def commit_npc_review(self, scope, world_id, preview_id):
        self.repository.world_actor_control_ids(scope, world_id)
        document = self._load_preview(scope, world_id, preview_id)
        if document.get("kind") != "npc_review":
            raise NotFound("NPC review preview not found")
        result, duplicate = self._commit_author_event_batch(scope, world_id, preview_id, document, "author.schedule_npc_review")
        return {"entity_id": document["entity_id"], "command_id": str(result.command_id),
                "state_version": str(result.state_version), "duplicate": duplicate}

    @staticmethod
    def _make_npc_review_event(state, scope, world_id, branch_id, preview_id, actor_id, count, interval_days):
        schedule, previous = plan_authored_npc_review(
            state, world_id, actor_id,
            max_runs=count, repeat_ticks=interval_days * state.calendar.day_seconds if count > 1 else None,
        )
        return NpcPlanReviewScheduledEvent(
            event_id=uuid5(preview_id, f"schedule-npc-review:{actor_id}"),
            world_id=world_id, branch_id=branch_id, command_id=preview_id, author_id=scope.user_id,
            actor_id=actor_id, expected_previous_revision=previous, schedule=schedule,
        )

    def _commit_author_event_batch(self, scope, world_id, preview_id, document, command_type):
        branch_id = UUID(document["branch_id"])
        key = f"author:{preview_id}"
        result = self.repository.read_command_receipt(scope, world_id, branch_id, preview_id, scope.user_id, key)
        if result is None:
            if datetime.now(timezone.utc) > datetime.fromisoformat(document["expires_at"]):
                raise Conflict("This author preview has expired; review a fresh preview")
            command = CommandEnvelope(
                command_id=preview_id, actor_id=scope.user_id, idempotency_key=key,
                expected_state_version=int(document["expected_version"]), command_type=command_type,
                payload={"preview_id": str(preview_id), "checkpoint_id": document["checkpoint_id"]},
            )
            result = self.repository.commit_command(
                scope, world_id, branch_id, command, [parse_event(event) for event in document["events"]],
            )
            duplicate = result.duplicate
        else:
            duplicate = True
        return result, duplicate

    def _store_preview(self, scope, world_id, preview_id, document):
        with self.operations.connect() as db:
            db.execute("INSERT INTO author_previews VALUES(?,?,?,?,?)", (*self.operations.identity(scope),
                       str(preview_id), str(world_id), json.dumps(document, ensure_ascii=False)))

    def _load_preview(self, scope, world_id, preview_id):
        with self.operations.connect() as db:
            row = db.execute("SELECT document FROM author_previews WHERE tenant=? AND principal=? AND id=? AND world_id=?",
                             (*self.operations.identity(scope), str(preview_id), str(world_id))).fetchone()
        if row is None:
            raise NotFound("Author preview not found")
        return json.loads(row[0])

    @staticmethod
    def _public(document):
        return {key: value for key, value in document.items() if key != "event"}
