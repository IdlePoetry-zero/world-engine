"""Compare two opening templates and apply safe authored changes to a new branch."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from hashlib import sha256
from uuid import UUID, uuid4, uuid5

from world_engine.domain.components import PersonalityLockViolation
from world_engine.domain.errors import InvalidEvent, NotFound, StaleVersion
from world_engine.domain.gameplay_events import AuthorComponentRevisedEvent
from world_engine.domain.models import AccessScope, CommandEnvelope, MembershipRole
from world_engine.domain.reducers import apply_event, author_value_hash

from .author_rule_checks import author_component_rule_errors
from .errors import ApplicationFailure
from .world_pack_import import WorldPackApplication
from .world_packs import load_world_pack


_AUTHOR_ROLES = {MembershipRole.OWNER, MembershipRole.EDITOR}
# These components describe authored definitions. Resources, life, injuries,
# skills earned in play and every history record stay under their own rules.
_MIGRATABLE = frozenset({"personality", "daily_routine", "attributes", "capabilities", "place", "production"})
_IMPORT_METADATA = frozenset({"creation_payload_hash", "world_pack_archive_sha256", "pack_version"})
_MAX_CHANGES = 128
_MISSING = object()


def _without_revision(value):
    if isinstance(value, dict):
        return {key: item for key, item in value.items() if key != "revision"}
    return value


def _template_version(value: str) -> tuple[int, ...]:
    return tuple(int(part) for part in value.split("."))


class TemplateMigrationApplication:
    def __init__(self, packs: WorldPackApplication, authoring):
        self.packs = packs
        self.authoring = authoring
        self.repository = packs.worlds.repository

    def preview(self, scope: AccessScope, world_id: UUID, branch_id: UUID, archive: bytes) -> dict:
        repository = self.repository
        repository.authorize_role(scope, world_id, _AUTHOR_ROLES)
        source = load_world_pack(self.packs.author_source(scope, world_id), engine_version=None)
        target = self.packs.prepare(archive)
        view = repository.read_world(scope, world_id, branch_id)
        original = repository.replay(scope, world_id, branch_id, through_seq=0).state
        source_version, target_version = source.manifest.version, target.package.manifest.version
        preview_id = uuid4()
        changes = []
        events = []

        def report(entity_id, entity_name, component, status, message, **details):
            changes.append({"entity_id": str(entity_id), "entity_name": entity_name, "component": component,
                            "status": status, "message": message} | details)

        def unsupported(component: str, message: str) -> None:
            report(world_id, view.world.title, component, "unsupported", message)

        if source.manifest.pack_id != target.package.manifest.pack_id or _template_version(target_version) <= _template_version(source_version):
            unsupported("pack_version", "请选择同一世界包中版本号更高的模板。")
        if target.draft is None:
            for blocker in target.blockers or ("有主线世界包需要选择开局并作为新世界导入。",):
                unsupported("compatibility", blocker)
        elif target.draft.ruleset_version != view.world.ruleset_version:
            unsupported("ruleset_version", "规则版本不同，需要专门的存档迁移程序。")
        if source.assets != target.package.assets:
            unsupported("assets", "模板资源已变化，需要单独导入以保存新版资源。")

        if not changes and target.draft is not None:
            proposed = target.draft.state
            for section in sorted(type(original).model_fields.keys() - {"entities", "metadata"}):
                if getattr(original, section) != getattr(proposed, section):
                    unsupported(section, "新版改动了开局状态或已有记录，需要专门的迁移规则。")
            old_metadata = {key: value for key, value in original.metadata.items() if key not in _IMPORT_METADATA}
            new_metadata = {key: value for key, value in proposed.metadata.items() if key not in _IMPORT_METADATA}
            if old_metadata != new_metadata:
                unsupported("metadata", "新版改动了开局配置或规则资料，需要专门的迁移规则。")

            working = view.state
            version_ref = {"kind": "pack", "reference_id": f"sha256:{sha256(archive).hexdigest()}",
                           "notes": f"模板 {source.manifest.pack_id} {source_version} → {target_version}"}
            for entity_id in sorted(original.entities.keys() | proposed.entities.keys()):
                before, after = original.entities.get(entity_id), proposed.entities.get(entity_id)
                if before is None or after is None:
                    entity = before or after
                    report(entity_id, entity.name or str(entity_id), "entity", "unsupported", "新版增删了实体，需单独处理已有引用。")
                    continue
                name = before.name or str(entity_id)
                if before.model_dump(exclude={"components"}) != after.model_dump(exclude={"components"}):
                    report(entity_id, name, "entity", "unsupported", "新版改动了名称、位置、状态或实体类型。")
                for component in sorted(before.components.keys() | after.components.keys()):
                    old = _without_revision(before.components.get(component))
                    new = _without_revision(after.components.get(component))
                    if old == new:
                        continue
                    entity = working.entities.get(entity_id)
                    current = entity.components.get(component) if entity is not None else None
                    if component not in _MIGRATABLE or not isinstance(new, dict):
                        report(entity_id, name, component, "unsupported", "该字段需要专门的迁移规则。")
                        continue
                    if entity is None or entity.status != "active" or entity.kind != before.kind:
                        report(entity_id, name, component, "conflict", "实例已失效、离世或改变类型。")
                        continue
                    original_fields = old or {}
                    current_fields = _without_revision(current) or {}
                    updated_fields = [key for key in sorted(original_fields.keys() | new.keys())
                                      if original_fields.get(key, _MISSING) != new.get(key, _MISSING)]
                    if component == "personality" and "locked_fields" in updated_fields:
                        report(entity_id, name, component, "unsupported", "请在人物设定中单独修改锁定状态。",
                               fields=["locked_fields"])
                        continue
                    conflicting = [key for key in updated_fields if current_fields.get(key, _MISSING) not in (
                        original_fields.get(key, _MISSING), new.get(key, _MISSING),
                    )]
                    pending = [key for key in updated_fields if current_fields.get(key, _MISSING) != new.get(key, _MISSING)]
                    if conflicting:
                        report(entity_id, name, component, "conflict", "当前实例已另行修改，请先核对差异。", fields=conflicting)
                    elif not pending:
                        report(entity_id, name, component, "already_applied", "当前实例已含此项更新。")
                    elif len(events) >= _MAX_CHANGES:
                        report(entity_id, name, component, "unsupported", "一次预览最多更新 128 项定义。")
                    else:
                        revision = current.get("revision", 0) if isinstance(current, dict) else 0
                        replacement = {key: value for key, value in current_fields.items() if key not in pending}
                        replacement.update({key: new[key] for key in pending if key in new})
                        replacement["revision"] = revision + 1
                        rule_errors = author_component_rule_errors(
                            working.metadata, working.entities, entity_id, entity.kind, component,
                            replacement, current=current,
                        )
                        if rule_errors:
                            for issue in rule_errors:
                                report(entity_id, name, component, "conflict", issue["message"], fields=[issue["path"]])
                            continue
                        event = AuthorComponentRevisedEvent(
                            entity_id=entity_id, author_id=scope.user_id, component_name=component,
                            expected_previous_hash=author_value_hash(current, component_name=component),
                            replacement=replacement, revision_note=version_ref["notes"],
                            provenance=[version_ref],
                        )
                        try:
                            working = apply_event(working, event)
                        except (InvalidEvent, ValueError) as error:
                            if isinstance(error.__cause__, PersonalityLockViolation):
                                report(entity_id, name, component, "conflict", "模板试图修改已锁定的人物设定。",
                                       fields=list(error.__cause__.fields))
                            else:
                                report(entity_id, name, component, "conflict", "更新未通过当前世界的约束与引用校验。")
                        else:
                            events.append(event.model_dump(mode="json"))
                            report(entity_id, name, component, "ready", "可在新分支应用。", fields=pending)

        can_apply = bool(events) and all(change["status"] in {"ready", "already_applied"} for change in changes)
        public = {"preview_id": str(preview_id), "source_version": source_version, "target_version": target_version,
                  "base_sequence": str(view.last_seq), "base_version": str(view.branch.state_version),
                  "changes": changes, "can_apply": can_apply}
        if can_apply:
            self.authoring._store_preview(scope, world_id, preview_id, {
                "template_migration": True, "source_branch_id": str(branch_id), "source_hash": view.state_hash,
                "expires_at": (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
                "migration_events": events, "source_version": source_version, "target_version": target_version,
                "base_version": public["base_version"], "base_sequence": public["base_sequence"],
            })
        return public

    def commit(self, scope: AccessScope, world_id: UUID, preview_id: UUID, label: str) -> dict:
        self.repository.authorize_role(scope, world_id, _AUTHOR_ROLES)
        document = self.authoring._load_preview(scope, world_id, preview_id)
        if document.get("template_migration") is not True:
            raise NotFound("Template update preview not found.")
        branch_id = uuid5(preview_id, "template-branch")
        command = CommandEnvelope(
            command_id=preview_id, actor_id=scope.user_id, idempotency_key=f"template-update:{preview_id}",
            expected_state_version=int(document["base_version"]), command_type="author.revise_component",
            payload={"preview_id": str(preview_id), "source_version": document["source_version"],
                     "target_version": document["target_version"]},
        )
        if datetime.now(timezone.utc) > datetime.fromisoformat(document["expires_at"]):
            receipt = self.repository.read_command_receipt(
                scope, world_id, branch_id, command.command_id, command.actor_id, command.idempotency_key,
            )
            if receipt is None:
                raise ApplicationFailure("TEMPLATE_PREVIEW_EXPIRED", "预览已过期，请重新检查当前分支。")
        try:
            result = self.repository.commit_template_fork(
                scope, world_id, UUID(document["source_branch_id"]), int(document["base_sequence"]),
                document["source_hash"], branch_id, label, command,
                [AuthorComponentRevisedEvent.model_validate(raw) for raw in document["migration_events"]],
            )
        except StaleVersion as error:
            raise ApplicationFailure("TEMPLATE_SOURCE_CHANGED", "来源分支已更新，请重新预览。") from error
        return {"branch_id": str(result.branch_id), "checkpoint_sequence": document["base_sequence"],
                "state_version": str(result.state_version), "duplicate": result.duplicate}
