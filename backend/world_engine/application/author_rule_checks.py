"""Check authored definitions against the selected world's current rules."""

from collections.abc import Mapping
from typing import TypedDict
from uuid import UUID

from pydantic import ValidationError

from world_engine.domain.components import (
    COMPONENT_REGISTRY, CapabilitiesComponent, PlaceComponent, PlaceRoute,
)

from .errors import ApplicationFailure


class AuthorFieldError(TypedDict):
    path: str
    type: str
    message: str


class AuthorRuleConflict(ApplicationFailure):
    def __init__(self, field_errors: list[AuthorFieldError]):
        super().__init__("AUTHOR_RULE_CONFLICT", "作者字段不符合当前世界规则。", status_code=422)
        self.field_errors = field_errors


def author_component_rule_errors(
    metadata: Mapping[str, object],
    entities: Mapping[UUID, object] | Mapping[str, object],
    entity_id: UUID,
    entity_kind: str,
    component_name: str,
    replacement: object,
    *,
    current: object = None,
    path_prefix: str | None = None,
) -> list[AuthorFieldError]:
    """Validate capability grants and place routes at author entry points.

    Omit ``current`` when validating a new world opening. An authored update
    can retain existing grants so editing old worlds remains possible.
    """
    if component_name not in {"capabilities", "place"}:
        return []
    if entity_kind not in COMPONENT_REGISTRY[component_name].entity_kinds:
        return []

    prefix = path_prefix or component_name
    schema = CapabilitiesComponent if component_name == "capabilities" else PlaceComponent
    try:
        parsed = schema.model_validate(replacement)
    except ValidationError as error:
        return _schema_errors(error, schema, prefix)

    if isinstance(parsed, CapabilitiesComponent):
        rules = metadata.get("content_rules")
        magic = rules.get("magic") if isinstance(rules, dict) else None
        if not isinstance(magic, dict) or magic.get("enabled") is not False:
            return []
        previous_grants = CapabilitiesComponent.model_validate(current).grants if current is not None else ()
        previous = set(previous_grants)
        return [
            {"path": f"{prefix}.grants.{index}", "type": "content_rules.magic.enabled",
             "message": "当前世界已禁用魔法，不能新增魔法或修炼能力。"}
            for index, grant in enumerate(parsed.grants)
            if grant.startswith(("magic.", "cultivation.")) and grant not in previous
        ]

    errors: list[AuthorFieldError] = []
    for index, route in enumerate(parsed.routes):
        route_path = f"{prefix}.routes.{index}"
        if route.from_place_id != entity_id:
            errors.append({
                "path": f"{route_path}.from_place_id", "type": "place.routes.from_place_id",
                "message": "路线的起点必须是其所属地点。",
            })
        target = entities.get(route.to_place_id)
        if target is None:
            target = entities.get(str(route.to_place_id))
        target_kind = target.get("kind") if isinstance(target, dict) else getattr(target, "kind", None)
        if target_kind not in ("place", "location"):
            errors.append({
                "path": f"{route_path}.to_place_id", "type": "place.routes.to_place_id",
                "message": "路线的目的地必须引用世界中已有的地点。",
            })
    return errors


def _schema_errors(
    error: ValidationError, schema: type[CapabilitiesComponent] | type[PlaceComponent], prefix: str,
) -> list[AuthorFieldError]:
    """Use registered field names and indexes without echoing input or error context."""
    errors: list[AuthorFieldError] = []
    for item in error.errors(include_url=False, include_input=False):
        location = item["loc"]
        path = [prefix]
        message = "字段格式或取值不符合组件规则。"
        validator_message = str(item.get("ctx", {}).get("error", ""))
        if location and location[0] in schema.model_fields:
            path.append(location[0])
            if len(location) > 1 and isinstance(location[1], int):
                path.append(str(location[1]))
        if location[:1] == ("routes",) and len(location) > 2 and location[2] in PlaceRoute.model_fields:
            path.append(location[2])
            if len(location) > 3 and isinstance(location[3], int):
                path.append(str(location[3]))
        elif validator_message == "a route must connect two different places":
            path.append("to_place_id")
            message = "路线的起点和目的地必须不同。"
        elif validator_message == "route ids must be unique within a place":
            path.append("routes")
            message = "同一地点中的路线标识必须唯一。"
        errors.append({"path": ".".join(path), "type": item["type"], "message": message})
    return errors
