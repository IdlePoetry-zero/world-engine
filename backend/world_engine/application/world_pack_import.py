"""Prepare a portable opening and register it as a fresh world."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from uuid import UUID, uuid5

from pydantic import ValidationError

from world_engine import __version__
from world_engine.domain.errors import Conflict, InvalidEvent, NotFound
from world_engine.domain.models import AccessScope, NewWorld, WorldState
from world_engine.domain.story_nodes import STORY_OPENING_METADATA
from world_engine.rules.versions import LEGACY_RULESET_VERSION, SUPPORTED_RULESET_VERSIONS

from .author_rule_checks import AuthorFieldError, author_component_rule_errors
from .errors import ApplicationFailure
from .world_packs import VerifiedWorldPack, WorldPackArchiveError, load_world_pack
from .worlds import WorldApplication


_IMPORT_NAMESPACE = UUID("388591b1-a102-472c-b181-e31ba09cb888")


@dataclass(frozen=True)
class PreparedWorldPack:
    package: VerifiedWorldPack
    draft: NewWorld | None
    blockers: tuple[str, ...]
    field_errors: tuple[AuthorFieldError, ...] = ()
    openings: tuple[dict, ...] = ()

    def report(self) -> dict:
        return {
            "manifest": self.package.manifest.model_dump(mode="json"),
            "compatibility": self.package.compatibility_report,
            "can_import": not self.blockers,
            "blockers": list(self.blockers),
            "contains_author_material": True,
            "field_errors": list(self.field_errors),
        } | ({"story_openings": list(self.openings)} if self.openings else {})


def _scenario_rule_errors(scenario: dict) -> tuple[AuthorFieldError, ...]:
    entities = scenario.get("entities")
    metadata = scenario.get("metadata", {})
    if not isinstance(entities, dict) or not isinstance(metadata, dict):
        return ()
    index = {}
    for key, entity in entities.items():
        try:
            entity_id = UUID(key)
        except (ValueError, TypeError, AttributeError):
            continue  # the state parser reports malformed entity keys
        if not isinstance(entity, dict) or not isinstance(entity.get("kind"), str):
            continue
        index[entity_id] = entity
    issues = []
    for entity_id, entity in index.items():
        components = entity.get("components")
        if not isinstance(components, dict):
            continue
        for component in ("capabilities", "place"):
            if component not in components:
                continue
            issues.extend(author_component_rule_errors(
                metadata, index, entity_id, entity["kind"], component, components[component],
                path_prefix=f"scenario.entities.{entity_id}.components.{component}",
            ))
            if len(issues) >= 32:
                return tuple(issues[:32])
    return tuple(issues)


class WorldPackApplication:
    def __init__(self, worlds: WorldApplication):
        self.worlds = worlds

    def prepare(self, data: bytes, *, opening_id: str | None = None) -> PreparedWorldPack:
        try:
            package = load_world_pack(data, engine_version=None)
        except WorldPackArchiveError as error:
            raise ApplicationFailure(error.code, str(error), status_code=422) from error

        manifest = package.manifest
        template = package.template
        blockers = []
        if not manifest.supports_engine(__version__):
            blockers.append("世界包需要其他引擎版本。")
        ruleset = template.get("ruleset_version") or LEGACY_RULESET_VERSION
        if ruleset not in SUPPORTED_RULESET_VERSIONS:
            blockers.append(f"当前引擎无法运行规则版本 {ruleset}。")
        installed = {item["pack_id"]: item for item in self.worlds.catalog.list_packs()}
        for dependency in manifest.dependencies:
            pack = installed.get(dependency.pack_id, {})
            versions = {pack.get("version"), *pack.get("available_versions", ()), *pack.get("sustained_available_versions", ())}
            if dependency.version not in versions:
                blockers.append(f"缺少世界包依赖 {dependency.pack_id} {dependency.version}。")
        if blockers:
            return PreparedWorldPack(package, None, tuple(blockers))

        openings = template.get("story_openings", ())
        if openings:
            return self._prepare_story_openings(package, ruleset, openings, opening_id)
        if opening_id is not None:
            return PreparedWorldPack(package, None, ("该世界包没有可选介入点。",))
        if template.get("scenario") is not None:
            issues = _scenario_rule_errors(template["scenario"])
            if issues:
                blockers = tuple(f"{issue['path']} · {issue['message']}" for issue in issues)
                return PreparedWorldPack(package, None, blockers, issues)
            try:
                opening = WorldState.model_validate_json(json.dumps(template["scenario"], ensure_ascii=True))
            except (ValidationError, ValueError, RecursionError):
                return PreparedWorldPack(package, None, ("开局数据的字段或引用未通过世界规则校验。",))
        else:
            try:
                generation = {
                    option: template[field]
                    for field, option in (("seed", "seed"), ("base_pack_version", "pack_version"),
                                          ("population_mode", "population_mode"))
                    if template.get(field) is not None
                }
                base = self.worlds.catalog.build_world(
                    template["base_pack_id"], manifest.title, "旅人", **generation,
                )
            except (KeyError, ValueError):
                return PreparedWorldPack(package, None, ("所选基础背景、版本或人口模式无法生成开局。",))
            opening = base.state
        if opening.player_id is None:
            return PreparedWorldPack(package, None, ("开局缺少可操控的玩家角色。",))
        try:
            validate = self.worlds.repository.validate_initial_world_state
            opening = validate(opening)
        except (InvalidEvent, ValidationError):
            return PreparedWorldPack(package, None, ("开局包含必须由已提交事件产生的记录。",))
        draft = NewWorld(title=manifest.title, state=opening, pack_version=manifest.version, ruleset_version=ruleset)
        return PreparedWorldPack(package, draft, ())

    def _prepare_story_openings(self, package, ruleset, openings, opening_id) -> PreparedWorldPack:
        summaries = []
        selected = None
        validate_initial = self.worlds.repository.validate_initial_world_state
        for item in openings:
            key = item["opening_id"]
            scenario = item["scenario"]
            issues = _scenario_rule_errors(scenario)
            if issues:
                blockers = tuple(f"{key} · {issue['path']} · {issue['message']}" for issue in issues)
                return PreparedWorldPack(package, None, blockers, issues)
            try:
                initial = validate_initial(WorldState.model_validate_json(json.dumps(scenario, ensure_ascii=True)))
            except (InvalidEvent, ValidationError, ValueError, RecursionError):
                return PreparedWorldPack(package, None, (f"介入点 {key} 的开局未通过世界规则校验。",))
            if initial.player_id is None:
                return PreparedWorldPack(package, None, (f"介入点 {key} 缺少可操控的玩家角色。",))
            summaries.append({field: item[field] for field in ("opening_id", "title", "position", "known_history")})
            if key == opening_id:
                metadata = initial.metadata | {STORY_OPENING_METADATA: key}
                selected = initial.model_copy(update={"metadata": metadata})
        if opening_id is not None and selected is None:
            return PreparedWorldPack(package, None, ("该介入点不在世界包中，请重新选择。",), openings=tuple(summaries))
        draft = None if selected is None else NewWorld(
            title=package.manifest.title, state=selected, pack_version=package.manifest.version, ruleset_version=ruleset,
        )
        return PreparedWorldPack(package, draft, (), openings=tuple(summaries))

    def preview(self, data: bytes) -> dict:
        return self.prepare(data).report() | {"archive_sha256": hashlib.sha256(data).hexdigest()}

    def import_world(
        self, scope: AccessScope, data: bytes, archive_hash: str, idempotency_key: str, opening_id: str | None = None,
    ) -> dict:
        digest = hashlib.sha256(data).hexdigest()
        if digest != archive_hash:
            raise ApplicationFailure("WORLD_PACK_PREVIEW_MISMATCH", "文件与预览版本不一致，请重新预览。", status_code=409)
        world_id = uuid5(_IMPORT_NAMESPACE, f"{scope.tenant_id}:{scope.user_id}:{idempotency_key}")
        repository = self.worlds.repository

        def confirmation(duplicate: bool) -> dict:
            view = repository.read_world(scope, world_id)
            original = repository.read_world_pack_archive(scope, world_id)
            if original is None or hashlib.sha256(original).hexdigest() != digest:
                raise Conflict("This idempotency key already identifies another world pack import.")
            if view.state.metadata.get(STORY_OPENING_METADATA) != opening_id:
                raise Conflict("This idempotency key already identifies another story opening.")
            return self.worlds._world_header(view) | {"duplicate": duplicate}

        try:
            return confirmation(True)
        except NotFound:
            pass
        prepared = self.prepare(data, opening_id=opening_id)
        if prepared.draft is None:
            if prepared.openings and not prepared.blockers:
                raise ApplicationFailure("STORY_OPENING_REQUIRED", "请先选择介入点。", status_code=422)
            raise ApplicationFailure("WORLD_PACK_INCOMPATIBLE", "; ".join(prepared.blockers), status_code=422)
        draft = prepared.draft
        metadata = dict(draft.state.metadata)
        metadata.pop("creation_payload_hash", None)
        if opening_id is None:
            metadata.pop(STORY_OPENING_METADATA, None)
        metadata["world_pack_archive_sha256"] = digest
        state = draft.state.model_copy(update={"metadata": metadata})
        world = draft.model_copy(update={
            "state": state, "world_id": world_id, "branch_id": uuid5(world_id, "main"),
        })
        try:
            repository.create_world(scope, world, world_pack_archive=data)
        except Conflict:
            return confirmation(True)
        return confirmation(False)

    def author_source(self, scope: AccessScope, world_id: UUID) -> bytes:
        archive = self.worlds.repository.read_world_pack_archive(scope, world_id)
        if archive is None:
            raise NotFound("No imported world pack is attached to this world.")
        return archive
