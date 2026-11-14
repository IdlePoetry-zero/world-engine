"""World use cases: idempotent creation, explicit author mode and safe views."""

from __future__ import annotations

import hashlib
import json
from typing import Any, Protocol
from uuid import UUID, uuid5

from world_engine.domain.errors import AccessDenied, Conflict, NotFound
from world_engine.domain.models import AccessScope, NewWorld, WorldView

from .visibility import project_player_state


_WORLD_NAMESPACE = UUID("38f144c5-c172-4ed6-a931-98c71341a808")


class PackCatalog(Protocol):
    def list_packs(self) -> list[dict[str, Any]]: ...

    def build_world(self, pack_id: str, title: str, player_name: str) -> NewWorld: ...


class WorldApplication:
    """Coordinates a repository and versioned content catalog.

    The repository remains responsible for tenant/membership enforcement. This
    service adds mode and viewpoint boundaries; raw state is internal by default.
    """

    def __init__(self, repository: Any, catalog: PackCatalog):
        self.repository = repository
        self.catalog = catalog

    def list_packs(self) -> dict[str, Any]:
        return {"items": self.catalog.list_packs()}

    def list_worlds(self, scope: AccessScope) -> dict[str, Any]:
        worlds = []
        for world in self.repository.list_worlds(scope):
            view = self.repository.read_world(scope, world.world_id)
            worlds.append(self._world_header(view))
        return {"items": worlds}

    def create_world(
        self, scope: AccessScope, pack_id: str, title: str, player_name: str, idempotency_key: str
    ) -> dict[str, Any]:
        if not idempotency_key or len(idempotency_key) > 200:
            raise ValueError("A bounded idempotency key is required.")
        payload = json.dumps(
            {"pack_id": pack_id, "title": title, "player_name": player_name},
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

        draft = self.catalog.build_world(pack_id, title, player_name)
        initial = draft.state.model_dump(mode="python")
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
        view = self.repository.read_world(scope, world_id, branch_id)
        state = view.state.model_dump(mode="json")
        if author:
            # Until the explicit editor membership method is integrated, fail
            # closed for anyone other than the world's owner.
            if view.world.owner_id != scope.user_id:
                raise AccessDenied("Only the owner can open the author projection.")
            projected = state
        else:
            actor_id = self.resolve_player(scope, view)
            projected = project_player_state(state, str(actor_id))
        return {
            "world": self._world_header(view),
            "branch": self._branch_header(view.branch),
            "state": projected,
            "last_seq": view.last_seq,
            "state_hash": view.state_hash,
            "mode": "author" if author else "player",
        }

    def resolve_player(self, scope: AccessScope, view: WorldView) -> UUID:
        if view.world.owner_id == scope.user_id:
            if view.state.player_id is None:
                raise AccessDenied("This world has no player character.")
            return view.state.player_id
        # Multiplayer sessions will select among repository-verified grants.
        # A raw client actor_id never selects a private viewpoint by itself.
        controls = self.repository.controlled_actors(scope, view.world.world_id)
        if len(controls) != 1:
            raise AccessDenied("An unambiguous controlled character is required.")
        return controls[0]

    def assert_player_actor(self, scope: AccessScope, view: WorldView, actor_id: UUID) -> None:
        if self.resolve_player(scope, view) != actor_id:
            raise AccessDenied("The requested actor is not this session's player.")

    def list_branches(self, scope: AccessScope, world_id: UUID) -> dict[str, Any]:
        return {"items": [self._branch_header(b) for b in self.repository.list_branches(scope, world_id)]}

    @staticmethod
    def _world_header(view: WorldView) -> dict[str, Any]:
        return {
            "world_id": view.world.world_id,
            "title": view.world.title,
            "active_branch_id": view.world.active_branch_id,
            "current_tick": view.branch.current_tick,
            "state_version": view.branch.state_version,
            "ruleset_version": view.world.ruleset_version,
            "status": "ready",
        }

    @staticmethod
    def _branch_header(branch: Any) -> dict[str, Any]:
        return {
            "branch_id": branch.branch_id, "label": branch.name,
            "current_tick": branch.current_tick, "state_version": branch.state_version,
        }

    def _created_response(self, view: WorldView, payload_hash: str, *, duplicate: bool) -> dict[str, Any]:
        if view.state.metadata.get("creation_payload_hash") != payload_hash:
            raise Conflict("An idempotency key was used with a different world creation request.")
        return self._world_header(view) | {"duplicate": duplicate}
