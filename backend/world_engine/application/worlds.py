"""World use cases: idempotent creation, explicit author mode and safe views."""

from __future__ import annotations

import hashlib
import json
from collections import OrderedDict
from threading import Lock
from typing import Any, Literal, Protocol
from uuid import UUID, uuid5

from world_engine.application.modern_opening_goal import enrich_modern_opening_goal
from world_engine.domain.components import PlaceComponent
from world_engine.domain.errors import AccessDenied, Conflict, InvalidEvent, NotFound, StaleVersion
from world_engine.domain.challenges import CHALLENGE_COMMAND, CHALLENGE_RESOLVED, open_challenges, own_report_receipts, settle_challenge
from world_engine.domain.contests import CONTEST_CHANGED, CONTEST_COMMAND, ContestAction, ContestRecord, contest_view, describe_contest, plan_contest
from world_engine.domain.leadership_ballots import leadership_ballot_is_invalidated
from world_engine.domain.npc_plan_review import NPC_STANDING_REVIEW_POLICY
from world_engine.domain.models import (
    AccessScope, BranchRecord, CommandEnvelope, MembershipRole, NewWorld, WorldEntity, WorldRecord, WorldState,
    WorldView,
)
from world_engine.domain.relationship_partnerships import eligible_partnership_proposal_ids
from world_engine.domain.story_nodes import STORY_OPENING_METADATA
from world_engine.rules.engine import route_accessible

from .errors import ApplicationFailure
from .safe_conduct_context import safe_conduct_action_context
from .combat_truce_context import combat_truce_options
from .cultivation_breakthrough_context import cultivation_breakthrough_options
from .institutional_credit_context import institutional_credit_context
from .institutional_finance_context import institutional_finance_context
from .shipment_relief_context import shipment_relief_options
from .visibility import project_player_state


_WORLD_NAMESPACE = UUID("38f144c5-c172-4ed6-a931-98c71341a808")
_PLAYER_PROJECTION_CACHE_MAX_ENTRIES = 24
_PLAYER_PROJECTION_CACHE_MAX_ENTRY_BYTES = 1_048_576
_PLAYER_PROJECTION_CACHE_MAX_BYTES = 8_388_608


class PackCatalog(Protocol):
    def list_packs(self) -> list[dict[str, Any]]: ...

    def build_world(
        self, pack_id: str, title: str, player_name: str, *,
        seed: int | None = None, population_mode: Literal["natural", "sustained"] = "natural",
        pack_version: str | None = None,
    ) -> NewWorld: ...


class WorldApplication:
    """Coordinates a repository and versioned content catalog.

    The repository remains responsible for tenant/membership enforcement. This
    service adds mode and viewpoint boundaries; raw state is internal by default.
    """

    def __init__(self, repository: Any, catalog: PackCatalog):
        self.repository = repository
        self.catalog = catalog
        self._player_projection_cache: OrderedDict[tuple[Any, ...], bytes] = OrderedDict()
        self._player_projection_cache_bytes = 0
        self._player_projection_cache_lock = Lock()

    def list_packs(self) -> dict[str, Any]:
        return {"items": self.catalog.list_packs()}

    def list_worlds(self, scope: AccessScope) -> dict[str, Any]:
        return {"items": [
            self._world_header_values(
                head.world,
                current_tick=head.current_tick,
                state_version=head.state_version,
            )
            for head in self.repository.list_world_heads(scope)
        ]}

    def create_world(
        self, scope: AccessScope, pack_id: str, title: str, player_name: str, idempotency_key: str, *,
        seed: int | None = None, population_mode: Literal["natural", "sustained"] = "natural",
        pack_version: str | None = None,
        npc_autonomy_mode: Literal["off", "limited_v1", "diverse_v1"] = "off",
    ) -> dict[str, Any]:
        if not idempotency_key or len(idempotency_key) > 200:
            raise ValueError("A bounded idempotency key is required.")
        generation_options: dict[str, Any] = {}
        if seed is not None:
            generation_options["seed"] = seed
        if population_mode != "natural":
            generation_options["population_mode"] = population_mode
        if pack_version is not None:
            generation_options["pack_version"] = pack_version
        creation_options = generation_options.copy()
        if npc_autonomy_mode != "off":
            creation_options["npc_autonomy_mode"] = npc_autonomy_mode
        payload = json.dumps(
            {"pack_id": pack_id, "title": title, "player_name": player_name}
            | creation_options,
            ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        )
        payload_hash = hashlib.sha256(payload.encode()).hexdigest()
        world_id = uuid5(_WORLD_NAMESPACE, f"{scope.tenant_id}:{scope.user_id}:{idempotency_key}")

        try:
            existing = self.repository.read_world(scope, world_id)
        except NotFound:
            existing = None
        if existing is not None:
            return self._created_response(existing, payload_hash, duplicate=True)

        if pack_version is not None:
            pack = next((item for item in self.catalog.list_packs() if item["pack_id"] == pack_id), None)
            offered = set(pack.get("available_versions", [pack.get("version")])) if pack is not None else set()
            if pack is not None and population_mode == "sustained":
                offered.update(pack.get("sustained_available_versions", ()))
            if pack_version not in offered:
                raise ApplicationFailure("PACK_VERSION_UNAVAILABLE", "该背景未提供所选内容版本。", status_code=422)
        try:
            draft = self.catalog.build_world(pack_id, title, player_name, **generation_options)
        except (KeyError, ValueError) as error:
            if npc_autonomy_mode == "diverse_v1":
                raise ApplicationFailure(
                    "NPC_AUTONOMY_PROFILE_UNAVAILABLE",
                    "所选背景无法使用此 NPC 自主模式。",
                    status_code=422,
                ) from error
            raise
        initial_state = draft.state
        if npc_autonomy_mode == "limited_v1":
            # The deterministic opening is keyed to the same authoritative world
            # identity used by persistence. It runs only for explicit opt-in.
            from world_engine.content.npc_autonomy_opening import enable_limited_npc_autonomy

            initial_state = enable_limited_npc_autonomy(initial_state, world_id=world_id)
        elif npc_autonomy_mode == "diverse_v1":
            from world_engine.content.npc_autonomy_opening import enable_diverse_npc_autonomy

            try:
                initial_state = enable_diverse_npc_autonomy(initial_state, world_id=world_id)
            except (TypeError, ValueError) as error:
                raise ApplicationFailure(
                    "NPC_AUTONOMY_PROFILE_UNAVAILABLE",
                    "所选背景无法使用此 NPC 自主模式。",
                    status_code=422,
                ) from error
        if initial_state.metadata.get(NPC_STANDING_REVIEW_POLICY) is True:
            from world_engine.content.npc_autonomy_opening import add_standing_npc_reviews

            initial_state = add_standing_npc_reviews(initial_state, world_id=world_id)
        initial = initial_state.model_dump(mode="python")
        initial["metadata"] = {**initial["metadata"], "creation_payload_hash": payload_hash}
        new_world = NewWorld(
            world_id=world_id,
            branch_id=uuid5(world_id, "main"),
            title=title,
            ruleset_version=draft.ruleset_version,
            pack_version=draft.pack_version,
            state=initial,
        )
        try:
            self.repository.create_world(scope, new_world)
        except Conflict:
            # A concurrent identical request may have won. The hash determines
            # whether it is a replay, never silently accept a different payload.
            existing = self.repository.read_world(scope, world_id)
            return self._created_response(existing, payload_hash, duplicate=True)
        return self._created_response(self.repository.read_world(scope, world_id), payload_hash, duplicate=False)

    def view_world(
        self, scope: AccessScope, world_id: UUID, branch_id: UUID | None = None, *, author: bool = False
    ) -> dict[str, Any]:
        if author:
            view = self.repository.read_world(scope, world_id, branch_id)
            state = view.state.model_dump(mode="json")
            self.repository.authorize_role(scope, world_id, {MembershipRole.OWNER, MembershipRole.EDITOR})
            state.setdefault("world_book_entries", [])
            projected = state | {"invalidated_leadership_ballot_ids": [
                str(ballot.ballot_id) for ballot in view.state.leadership_ballots
                if leadership_ballot_is_invalidated(view.state, ballot)
            ]}
            return self._world_view_response(view, projected, mode="author")
        return self._view_player_world(scope, world_id, branch_id)

    def _view_player_world(
        self, scope: AccessScope, world_id: UUID, branch_id: UUID | None,
    ) -> dict[str, Any]:
        def project(view: WorldView) -> dict[str, Any]:
            # The repository lends a verified snapshot for this read. Only the
            # detached player projection leaves this callback.
            actor_id = self.resolve_player_identity(scope, view.world, view.state.player_id)
            cache_key = self._player_projection_key(scope, view.world, view.branch, actor_id)
            projected = self._read_cached_player_projection(cache_key)
            if projected is None:
                projected = self._build_player_projection(scope, view, actor_id)
                self._store_player_projection(cache_key, projected)
            self._add_fresh_truce_authorization(scope, world_id, view.world, view.state.player_id, projected)
            if view.state.contest_definitions:
                choices = contest_view(view.state, actor_id)
                for row in (row for choice in choices for row in choice["recent"] if row["can_ask_npc"]):
                    recipient = UUID(row["recipient_id"])
                    row["can_ask_npc"] = False
                    if recipient != view.state.player_id and scope.user_id == view.world.owner_id:
                        try:
                            self.repository.authorize_hosted_npc(scope, world_id, recipient)
                            row["can_ask_npc"] = True
                        except AccessDenied:
                            pass
                if choices:
                    projected["local_contests"] = choices
            return self._world_view_response(view, projected, mode="player")

        return self.repository.project_verified_player_view(scope, world_id, branch_id, project)

    @staticmethod
    def _player_projection_key(
        scope: AccessScope, world: WorldRecord, branch: BranchRecord, actor_id: UUID,
    ) -> tuple[Any, ...]:
        return (
            scope.tenant_id, scope.user_id, world.owner_id == scope.user_id,
            world.world_id, world.owner_id, world.ruleset_version, world.pack_version,
            branch.branch_id, branch.state_hash,
            branch.state_version, branch.current_tick, actor_id,
        )

    def _read_cached_player_projection(self, key: tuple[Any, ...]) -> dict[str, Any] | None:
        with self._player_projection_cache_lock:
            serialized = self._player_projection_cache.get(key)
            if serialized is not None:
                self._player_projection_cache.move_to_end(key)
        if serialized is None:
            return None
        # Decode outside the lock to keep cache critical sections short. The new
        # object graph is detached, so response handlers cannot mutate the cache.
        value = json.loads(serialized)
        return value if isinstance(value, dict) else None

    def _store_player_projection(self, key: tuple[Any, ...], projected: dict[str, Any]) -> None:
        serialized = json.dumps(projected, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        size = len(serialized)
        if size > _PLAYER_PROJECTION_CACHE_MAX_ENTRY_BYTES:
            return
        with self._player_projection_cache_lock:
            previous = self._player_projection_cache.pop(key, None)
            if previous is not None:
                self._player_projection_cache_bytes -= len(previous)
            while self._player_projection_cache and (
                len(self._player_projection_cache) >= _PLAYER_PROJECTION_CACHE_MAX_ENTRIES
                or self._player_projection_cache_bytes + size > _PLAYER_PROJECTION_CACHE_MAX_BYTES
            ):
                _, evicted = self._player_projection_cache.popitem(last=False)
                self._player_projection_cache_bytes -= len(evicted)
            self._player_projection_cache[key] = serialized
            self._player_projection_cache_bytes += size

    def _build_player_projection(
        self, scope: AccessScope, view: WorldView, actor_id: UUID,
    ) -> dict[str, Any]:
        state = view.state.model_dump(mode="json")
        projected = project_player_state(state, str(actor_id))
        enrich_modern_opening_goal(self.repository, scope, view, actor_id, state, projected)
        if view.state.challenges:
            choices = open_challenges(view.state, actor_id)
            if choices:
                projected["local_challenges"] = choices
            if receipts := own_report_receipts(view.state, actor_id):
                projected["own_report_receipts"] = receipts
        credit = institutional_credit_context(
            view.state, actor_id, visible_organization_ids=set(map(UUID, projected["entities"])),
        )
        if credit["represented_institutions"]:
            projected["institutional_credit"] = credit
            projected["institutional_finance"] = institutional_finance_context(view.state, actor_id, credit)
        breakthrough_options = cultivation_breakthrough_options(view, actor_id)
        if breakthrough_options:
            projected["cultivation_breakthrough_options"] = breakthrough_options
        truce = combat_truce_options(view.state, actor_id, projected["entities"])
        if truce is not None:
            # can_request_response is repository authorization, not a state
            # projection. It is attached after each cache read.
            projected["combat_truce_options"] = truce
        projected["partnership_proposal_target_ids"] = [
            str(target) for target in eligible_partnership_proposal_ids(
                view.state, actor_id, map(UUID, projected["entities"]),
            )
        ]
        projected["shipment_relief_options"] = shipment_relief_options(
            view.state, actor_id, set(map(UUID, projected["entities"])),
        )
        actor = view.state.entities[actor_id]
        origin = view.state.entities.get(actor.location_id)
        raw_place = origin.components.get("place") if origin is not None else None
        projected["travel_options"] = []
        if raw_place is not None and actor.status == "active":
            for route in PlaceComponent.model_validate(raw_place).routes:
                destination = view.state.entities.get(route.to_place_id)
                if (
                    destination is None
                    or (destination.components.get("hidden") is True and str(destination.id) not in projected["entities"])
                    or not route_accessible(view.state, actor, route, destination)
                ):
                    continue
                projected["travel_options"].append({
                    "route_id": str(route.route_id), "destination_name": destination.name or str(destination.id),
                    "travel_ticks": str(route.travel_ticks),
                })
        projected["itinerary_route_options"] = self.itinerary_route_options(
            view.state, actor, projected.get("entities", {}),
        )
        projected.update(safe_conduct_action_context(
            view.state, projected, str(actor_id), can_host=scope.user_id == view.world.owner_id,
        ))
        return projected

    def _add_fresh_truce_authorization(
        self, scope: AccessScope, world_id: UUID, world: WorldRecord, player_id: UUID | None,
        projected: dict[str, Any],
    ) -> None:
        truce = projected.get("combat_truce_options")
        if not isinstance(truce, dict):
            return
        can_request_response = False
        if truce.get("proposed_by_self") and scope.user_id == world.owner_id:
            opponent_id = UUID(truce["opponent_id"])
            if opponent_id != player_id:
                try:
                    self.repository.authorize_hosted_npc(scope, world_id, opponent_id)
                    can_request_response = True
                except AccessDenied:
                    pass
        projected["combat_truce_options"] = truce | {"can_request_response": can_request_response}


    @staticmethod
    def itinerary_route_options(
        state: WorldState,
        actor: WorldEntity,
        projected_entities: object,
    ) -> list[dict[str, str]]:
        """Return currently traversable outward edges from player-known places.

        These records help select an ordered route list. They grant no lasting
        access: ordinary travel rules recheck visibility, route status, and actor
        permissions when each leg departs.
        """

        if actor.status != "active" or not isinstance(projected_entities, dict):
            return []

        origins: list[WorldEntity] = []
        for entity_id, projection in projected_entities.items():
            if not isinstance(entity_id, str) or not isinstance(projection, dict):
                continue
            if projection.get("kind") not in {"place", "location"}:
                continue
            try:
                place_id = UUID(entity_id)
            except ValueError:
                continue
            place = state.entities.get(place_id)
            if (
                place is not None
                and place.kind in {"place", "location"}
                and place.status == "active"
            ):
                origins.append(place)

        options: list[dict[str, str]] = []
        for origin in sorted(origins, key=lambda item: (item.name or str(item.id), str(item.id))):
            try:
                origin_place = PlaceComponent.model_validate(origin.components.get("place", {}))
            except (TypeError, ValueError):
                continue
            hypothetical_actor = actor.model_copy(update={"location_id": origin.id})
            for route in origin_place.routes:
                destination = state.entities.get(route.to_place_id)
                if (
                    destination is None
                    or (destination.components.get("hidden") is True
                        and str(destination.id) not in projected_entities)
                    or not route_accessible(state, hypothetical_actor, route, destination)
                ):
                    continue
                options.append({
                    "route_id": str(route.route_id),
                    "from_place_id": str(origin.id),
                    "from_place_name": origin.name or str(origin.id),
                    "to_place_id": str(destination.id),
                    "to_place_name": destination.name or str(destination.id),
                    "travel_ticks": str(route.travel_ticks),
                })

        return sorted(options, key=lambda item: (
            item["from_place_name"].casefold(), item["from_place_id"],
            item["to_place_name"].casefold(), item["to_place_id"], item["route_id"],
        ))

    @staticmethod
    def _world_view_response(view: WorldView, projected: dict[str, Any], *, mode: str) -> dict[str, Any]:
        return {
            "world": WorldApplication._world_header(view),
            "branch": WorldApplication._branch_header(view.branch),
            "state": projected,
            "last_seq": view.last_seq,
            "state_hash": view.state_hash,
            "mode": mode,
        }

    def resolve_player(self, scope: AccessScope, view: WorldView) -> UUID:
        return self.resolve_player_identity(scope, view.world, view.state.player_id)

    def resolve_player_identity(
        self, scope: AccessScope, world: WorldRecord, default_player_id: UUID | None,
    ) -> UUID:
        if world.owner_id == scope.user_id:
            if default_player_id is None:
                raise AccessDenied("This world has no player character.")
            return default_player_id
        # Multiplayer sessions will select among repository-verified grants.
        # A raw client actor_id never selects a private viewpoint by itself.
        controls = self.repository.controlled_actors(scope, world.world_id)
        if len(controls) != 1:
            raise AccessDenied("An unambiguous controlled character is required.")
        return controls[0]

    def assert_player_actor(self, scope: AccessScope, view: WorldView, actor_id: UUID) -> None:
        if self.resolve_player(scope, view) != actor_id:
            raise AccessDenied("The requested actor is not this session's player.")

    def continuation_options(self, scope: AccessScope, world_id: UUID, branch_id: UUID) -> dict[str, Any]:
        from world_engine.domain.player_continuation import is_player_continuation_candidate

        self.repository.authorize_role(scope, world_id, {MembershipRole.OWNER})
        view = self.repository.read_world(scope, world_id, branch_id)
        previous = view.state.entities.get(view.state.player_id)
        result = {"previous_player_id": view.state.player_id, "state_version": str(view.branch.state_version), "items": []}
        if previous is None or previous.status != "dead":
            return result
        if previous.id not in self.repository.find_death_events(scope, world_id, branch_id, {previous.id}):
            return result

        unavailable = self.repository.unavailable_continuation_actor_ids(scope, world_id)
        candidates = [entity for entity in view.state.entities.values()
                      if entity.id not in unavailable and entity.components.get("hidden") is not True
                      and is_player_continuation_candidate(view.state, entity.id)]
        result["items"] = [{"actor_id": entity.id, "name": entity.name, "role": entity.components.get("public_role")}
                           for entity in sorted(candidates, key=lambda item: (item.name.casefold(), str(item.id)))]
        return result

    def continue_after_death(
        self, scope: AccessScope, world_id: UUID, branch_id: UUID, previous_player_id: UUID, new_player_id: UUID,
        expected_state_version: int, idempotency_key: str,
    ) -> dict[str, Any]:
        from world_engine.domain.player_continuation import plan_player_character_continuation

        self.repository.authorize_role(scope, world_id, {MembershipRole.OWNER})
        key = "continue:" + hashlib.sha256(idempotency_key.encode()).hexdigest()
        command_id = uuid5(world_id, f"player-continuation:{scope.user_id}:{branch_id}:{key}")
        result = self.repository.read_command_receipt(scope, world_id, branch_id, command_id, previous_player_id, key)
        if result is not None:
            event, = (item for item in result.events if item.type == "core.player_character_continued.v1")
            if (event.type != "core.player_character_continued.v1"
                or event.payload["new_player_id"] != str(new_player_id)
                or result.state_version != expected_state_version + 1):
                raise Conflict("This continuation key was already used for a different action.")
        else:
            view = self.repository.read_world(scope, world_id, branch_id)
            if view.branch.state_version != expected_state_version:
                raise StaleVersion(expected_state_version, view.branch.state_version)
            source = self.repository.find_death_events(scope, world_id, branch_id, {previous_player_id}).get(previous_player_id)
            if source is None:
                raise ApplicationFailure("PLAYER_DEATH_NOT_RECORDED", "当前分支没有可核实的原角色死亡记录。", status_code=409)
            event = plan_player_character_continuation(
                view.state, world_id=world_id, branch_id=branch_id, command_id=command_id,
                ruleset_version=view.world.ruleset_version, actor_id=previous_player_id,
                previous_player_id=previous_player_id, new_player_id=new_player_id, source_death_event_id=source,
            )
            command = CommandEnvelope(command_id=command_id, actor_id=previous_player_id, idempotency_key=key,
                expected_state_version=expected_state_version, command_type="player.continue_after_death",
                payload={"previous_player_id": str(previous_player_id), "new_player_id": str(new_player_id),
                         "source_death_event_id": str(source)}, causation_ids=[source])
            result = self.repository.commit_command(scope, world_id, branch_id, command, [event])
        return {"branch_id": branch_id, "player_id": new_player_id, "state_version": str(result.state_version),
                "command_id": str(result.command_id)}

    def perform_challenge(
        self, scope: AccessScope, world_id: UUID, branch_id: UUID, actor_id: UUID, challenge_id: UUID,
        expected_state_version: int, idempotency_key: str,
    ) -> dict[str, Any]:
        key = "challenge:" + hashlib.sha256(idempotency_key.encode()).hexdigest()
        command_id = uuid5(world_id, f"{scope.user_id}:{branch_id}:{key}")
        result = self.repository.read_command_receipt(scope, world_id, branch_id, command_id, actor_id, key)
        if result is None:
            view = self.repository.read_world(scope, world_id, branch_id)
            if self.resolve_player(scope, view) != actor_id:
                raise AccessDenied("This challenge requires the current player character.")
            if view.branch.state_version != expected_state_version:
                result = self.repository.read_command_receipt(scope, world_id, branch_id, command_id, actor_id, key)
                if result is None:
                    raise StaleVersion(expected_state_version, view.branch.state_version)
        if result is None:
            try:
                event = settle_challenge(view.state, challenge_id, actor_id, uuid5(command_id, "challenge.result"))
            except InvalidEvent as error:
                raise ApplicationFailure("CHALLENGE_UNAVAILABLE", str(error), status_code=409) from error
            command = CommandEnvelope(command_id=command_id, actor_id=actor_id, idempotency_key=key,
                expected_state_version=expected_state_version, command_type=CHALLENGE_COMMAND,
                payload={"challenge_id": str(challenge_id)})
            result = self.repository.commit_command(scope, world_id, branch_id, command, [event])
        receipt, = (item for item in result.events if item.type == CHALLENGE_RESOLVED)
        attempt = receipt.payload["attempt"]
        if attempt["challenge_id"] != str(challenge_id) or result.state_version != expected_state_version + 1:
            raise Conflict("This challenge key was already used for a different action.")
        return {"challenge_id": challenge_id, "passed": attempt["passed"], "state_version": str(result.state_version)}

    def list_branches(self, scope: AccessScope, world_id: UUID) -> dict[str, Any]:
        return {"items": [self._branch_header(b) for b in self.repository.list_branches(scope, world_id)]}

    def change_contest(
        self, scope: AccessScope, world_id: UUID, branch_id: UUID, actor_id: UUID, action: ContestAction,
        expected_state_version: int, idempotency_key: str,
    ) -> dict[str, Any]:
        key = "contest:" + hashlib.sha256(idempotency_key.encode()).hexdigest()
        command_id = uuid5(world_id, f"{scope.user_id}:{branch_id}:{key}")
        payload = action.model_dump(mode="json")
        result = self.repository.read_command_receipt(scope, world_id, branch_id, command_id, actor_id, key)
        if result is None:
            view = self.repository.read_world(scope, world_id, branch_id)
            if self.resolve_player(scope, view) != actor_id:
                raise AccessDenied("This contest operation requires the current player character.")
            if view.branch.state_version != expected_state_version:
                result = self.repository.read_command_receipt(scope, world_id, branch_id, command_id, actor_id, key)
                if result is None:
                    raise StaleVersion(expected_state_version, view.branch.state_version)
        if result is None:
            try:
                event = plan_contest(view.state, actor_id, action, uuid5(command_id, "contest.change"))
            except InvalidEvent as error:
                raise ApplicationFailure("CONTEST_UNAVAILABLE", str(error), status_code=409) from error
            command = CommandEnvelope(command_id=command_id, actor_id=actor_id, idempotency_key=key,
                expected_state_version=expected_state_version, command_type=CONTEST_COMMAND, payload=payload)
            result = self.repository.commit_command(scope, world_id, branch_id, command, [event])
        receipt, = (item for item in result.events if item.type == CONTEST_CHANGED)
        if receipt.payload["action"] != payload or result.state_version != expected_state_version + 1:
            raise Conflict("This contest key was already used for a different action.")
        return describe_contest(ContestRecord.model_validate(receipt.payload["contest"])) | {"state_version": str(result.state_version)}

    def fork_branch(
        self, scope: AccessScope, world_id: UUID, source_id: UUID, sequence: int,
        label: str, idempotency_key: str,
    ) -> dict[str, Any]:
        self.repository.authorize_role(scope, world_id, {MembershipRole.OWNER, MembershipRole.EDITOR})
        branch_id = uuid5(world_id, f"fork:{scope.user_id}:{idempotency_key}")
        branch = self.repository.fork(scope, world_id, source_id, sequence, label, branch_id=branch_id)
        return self._branch_header(branch)

    @staticmethod
    def _world_header(view: WorldView) -> dict[str, Any]:
        header = WorldApplication._world_header_values(
            view.world,
            current_tick=view.branch.current_tick,
            state_version=view.branch.state_version,
        )
        if STORY_OPENING_METADATA in view.state.metadata:
            header["world_mode"] = "story"
        return header

    @staticmethod
    def _world_header_values(world: Any, *, current_tick: int, state_version: int) -> dict[str, Any]:
        return {
            "world_id": world.world_id,
            "title": world.title,
            "active_branch_id": world.active_branch_id,
            "current_tick": current_tick,
            "state_version": state_version,
            "ruleset_version": world.ruleset_version,
            "pack_version": world.pack_version,
            "status": "ready",
        }

    @staticmethod
    def _branch_header(branch: Any) -> dict[str, Any]:
        return {
            "branch_id": branch.branch_id, "label": branch.name,
            "current_tick": branch.current_tick, "state_version": branch.state_version,
            "head_seq": branch.head_seq,
        }

    def _created_response(self, view: WorldView, payload_hash: str, *, duplicate: bool) -> dict[str, Any]:
        if view.state.metadata.get("creation_payload_hash") != payload_hash:
            raise Conflict("An idempotency key was used with a different world creation request.")
        return self._world_header(view) | {"duplicate": duplicate}
