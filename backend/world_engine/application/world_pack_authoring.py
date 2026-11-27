"""Pure authoring preview and export for portable world-pack templates.

The caller supplies an explicit, author-approved v1 template. This module has
no repository or branch input: it never turns a live ``WorldView`` into
``template.scenario`` implicitly.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from pydantic import ValidationError

from world_engine.application.author_rule_checks import AuthorFieldError, author_component_rule_errors
from world_engine.application.world_packs import (
    VerifiedWorldPack,
    WorldPackArchiveError,
    WorldPackLimits,
    WorldPackManifest,
    load_world_pack,
    write_world_pack,
)
from world_engine.domain.json_types import JsonObject
from world_engine.domain.models import WorldState


class WorldPackAuthoringError(WorldPackArchiveError):
    """An author-provided template is valid JSON but is not a portable opening."""

    def __init__(
        self,
        message: str,
        *,
        code: str = "WORLD_PACK_AUTHORING_INVALID",
        field_errors: tuple[AuthorFieldError, ...] = (),
    ):
        super().__init__(message, code=code)
        self.field_errors = field_errors


@dataclass(frozen=True, slots=True)
class WorldPackAuthoringPreview:
    """A validated ZIP and its safe summary, ready for display or download."""

    archive: bytes
    archive_sha256: str
    verified: VerifiedWorldPack
    report: JsonObject


class WorldPackAuthoringApplication:
    """Validate and package caller-selected authored content using world-pack v1."""

    def preview(
        self,
        *,
        manifest: WorldPackManifest | Mapping[str, Any],
        template: JsonObject,
        assets: dict[str, bytes] | None = None,
        limits: WorldPackLimits = WorldPackLimits(),
    ) -> WorldPackAuthoringPreview:
        """Validate a portable author template and verify a write/read roundtrip.

        ``template`` must be assembled from the author's chosen content. If it
        includes a ``scenario``, that value means authored starting content;
        it is not a snapshot/export of the current branch. The service has no
        branch or repository dependency by design.
        """
        try:
            checked_manifest = WorldPackManifest.model_validate(manifest)
        except ValidationError:
            raise WorldPackAuthoringError("世界包清单字段无效。") from None

        checked_template = _json_template(template)
        self._validate_template_openings(checked_template)

        try:
            archive = write_world_pack(
                manifest=checked_manifest,
                template=checked_template,
                assets=assets,
                limits=limits,
            )
            verified = load_world_pack(archive, limits=limits)
        except WorldPackArchiveError:
            raise

        digest = hashlib.sha256(archive).hexdigest()
        opening_count = (
            1 if "scenario" in checked_template else len(checked_template.get("story_openings", ()))
        )
        report: JsonObject = {
            "can_export": True,
            "roundtrip_verified": True,
            "archive_sha256": digest,
            "manifest": verified.manifest.model_dump(mode="json"),
            "compatibility": verified.compatibility_report,
            "opening_count": opening_count,
            "asset_count": len(verified.assets),
            "asset_bytes": sum(map(len, verified.assets.values())),
        }
        return WorldPackAuthoringPreview(archive, digest, verified, report)

    def export(
        self,
        *,
        manifest: WorldPackManifest | Mapping[str, Any],
        template: JsonObject,
        assets: dict[str, bytes] | None = None,
        limits: WorldPackLimits = WorldPackLimits(),
    ) -> bytes:
        """Return the same validated archive produced by ``preview``."""
        return self.preview(manifest=manifest, template=template, assets=assets, limits=limits).archive

    @staticmethod
    def _validate_template_openings(template: JsonObject) -> None:
        scenario = template.get("scenario")
        if scenario is not None:
            _validate_authored_scenario(scenario, path="scenario")

        openings = template.get("story_openings", ())
        if not isinstance(openings, (list, tuple)):
            return  # The archive contract reports its schema error with the normal archive code.
        for position, opening in enumerate(openings):
            if not isinstance(opening, dict) or "scenario" not in opening:
                continue  # The archive contract reports malformed opening objects.
            _validate_authored_scenario(opening["scenario"], path=f"story_openings.{position}.scenario")


def _json_template(value: object) -> JsonObject:
    if not isinstance(value, Mapping):
        raise WorldPackAuthoringError("世界包模板必须是 JSON 对象。")
    template = dict(value)
    if isinstance(template.get("scenario"), WorldState):
        template["scenario"] = template["scenario"].model_dump(mode="json")

    openings = template.get("story_openings")
    if isinstance(openings, (list, tuple)):
        normalized_openings = []
        for opening in openings:
            if isinstance(opening, Mapping):
                item = dict(opening)
                if isinstance(item.get("scenario"), WorldState):
                    item["scenario"] = item["scenario"].model_dump(mode="json")
                normalized_openings.append(item)
            else:
                normalized_openings.append(opening)
        template["story_openings"] = normalized_openings

    try:
        encoded = json.dumps(template, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
        copied = json.loads(encoded)
    except (TypeError, ValueError, UnicodeError, RecursionError):
        raise WorldPackAuthoringError("世界包模板无法编码为有限 JSON 数据。") from None
    if not isinstance(copied, dict):
        raise WorldPackAuthoringError("世界包模板必须是 JSON 对象。")
    return copied


def _validate_authored_scenario(value: object, *, path: str) -> None:
    if isinstance(value, WorldState):
        raw = value.model_dump(mode="json")
    elif isinstance(value, Mapping):
        raw = dict(value)
    else:
        raise WorldPackAuthoringError(f"{path} 必须是作者提供的开局状态。")

    try:
        encoded = json.dumps(raw, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
        state = WorldState.model_validate_json(encoded)
    except (TypeError, ValueError, UnicodeError, ValidationError, RecursionError):
        raise WorldPackAuthoringError(f"{path} 的数据字段或引用未通过世界规则校验。") from None

    forbidden = []
    if state.story_milestones:
        forbidden.append("story_milestones")
    if state.ambient_events:
        forbidden.append("ambient_events")
    if state.challenge_attempts:
        forbidden.append("challenge_attempts")
    if any(record.speech_reply_to is not None for record in state.knowledge):
        forbidden.append("knowledge.speech_reply_to")
    if any(record.topic_inspection is not None for record in state.knowledge):
        forbidden.append("knowledge.topic_inspection")
    for field in (
        "contests", "npc_plans", "relationship_invitations", "institutional_credit_contracts",
        "private_gifts", "relationship_reflections", "relationship_partnerships",
        "public_accusation_corrections", "accusation_repair_offers", "leadership_ballots",
        "leadership_vacancy_reviews", "safe_conduct_treaties", "npc_attendance", "travel_journeys",
    ):
        if getattr(state, field):
            forbidden.append(field)
    if state.promise_trust is not None:
        forbidden.append("promise_trust")
    if any(promise.review_receipt is not None for promise in state.relationship_promises):
        forbidden.append("relationship_promises.review_receipt")
    if forbidden:
        raise WorldPackAuthoringError(
            f"{path} 包含只能由已提交事件产生的存档事实。",
            field_errors=tuple(
                {"path": f"{path}.{field}", "type": "scenario.committed_outcome", "message": "开局不能预置已提交事件结果。"}
                for field in forbidden
            ),
        )
    if state.player_id is None:
        raise WorldPackAuthoringError(
            f"{path} 缺少可操控的玩家角色。",
            field_errors=({"path": f"{path}.player_id", "type": "scenario.player_id", "message": "开局需要指定玩家角色。"},),
        )

    issues = _scenario_rule_errors(raw, path=path)
    if issues:
        raise WorldPackAuthoringError(
            f"{path} 中有不符合作者规则的字段。",
            field_errors=tuple(issues),
        )


def _scenario_rule_errors(
    scenario: dict[str, object], *, path: str,
) -> tuple[AuthorFieldError, ...]:
    entities = scenario.get("entities")
    metadata = scenario.get("metadata", {})
    if not isinstance(entities, dict) or not isinstance(metadata, dict):
        return ()

    index: dict[UUID, dict[str, object]] = {}
    to_check: list[tuple[UUID, dict[str, object], str]] = []
    for key, entity in entities.items():
        try:
            entity_id = UUID(str(key))
        except (ValueError, TypeError, AttributeError):
            continue  # WorldState validation reports malformed entity keys.
        if not isinstance(entity, dict) or not isinstance(entity.get("kind"), str):
            continue
        index[entity_id] = entity
        to_check.append((entity_id, entity, f"{path}.entities.{entity_id}"))

    arrivals = scenario.get("story_arrivals", [])
    if isinstance(arrivals, list):
        for position, entry in enumerate(arrivals):
            actor = entry.get("actor") if isinstance(entry, dict) else None
            if not isinstance(actor, dict) or not isinstance(actor.get("kind"), str):
                continue
            try:
                entity_id = UUID(str(actor.get("id", "")))
            except (ValueError, TypeError, AttributeError):
                continue
            index[entity_id] = actor
            to_check.append((entity_id, actor, f"{path}.story_arrivals.{position}.actor"))

    issues = []
    for entity_id, entity, prefix in to_check:
        components = entity.get("components")
        if not isinstance(components, dict):
            continue
        for component in ("capabilities", "place"):
            if component not in components:
                continue
            issues.extend(author_component_rule_errors(
                metadata, index, entity_id, str(entity["kind"]), component, components[component],
                path_prefix=f"{prefix}.components.{component}",
            ))
            if len(issues) >= 32:
                return tuple(issues[:32])
    return tuple(issues)
