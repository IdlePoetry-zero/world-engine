"""Compile compact, alias-based authored content into verified world packs.

The compiler accepts the existing ``{manifest, template}`` world-pack shape.
Inside an authored scenario, ``entities`` may be an array with an ``alias`` on
each row. Entity references use the explicit object ``{"$entity": "alias"}``.
Place routes may declare ``alias`` and then receive ``route_id`` values from
the same deterministic UUIDv5 namespace; references use ``{"$route": "alias"}``.
The alias mode is opt-in. A normal portable scenario with an entity dictionary
continues to pass through unchanged.

No repository, branch, or live WorldView is accepted by this module.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid5

from world_engine.application.character_cards import character_card_draft
from world_engine.application.world_book_import import load_world_book
from world_engine.application.world_pack_authoring import WorldPackAuthoringApplication
from world_engine.domain.errors import InvalidEvent
from world_engine.domain.json_types import JsonObject


_ALIAS_PATTERN = re.compile(r"^[A-Za-z][A-Za-z0-9_.-]{0,159}$")
_PACK_ID_PATTERN = re.compile(r"^[A-Za-z][A-Za-z0-9_.-]{0,159}$")
_OPENING_ID_PATTERN = re.compile(r"^[A-Za-z][A-Za-z0-9_.-]{0,79}$")
_PLACE_REFERENCE_FIELDS = {
    "location_id", "place_id", "region_id", "from_place_id", "to_place_id",
    "origin_place_id", "destination_place_id", "venue_id",
    "known_places",
}
_CHARACTER_REFERENCE_FIELDS = {"player_id"}
_DSL_FIELDS = {"character_cards", "world_book_adapter"}


class AuthoredWorldPackBuildError(ValueError):
    """Invalid aliases or adapter input, with a location in the source JSON."""

    def __init__(self, message: str, *, path: str = ""):
        super().__init__(message)
        self.path = path


@dataclass(frozen=True, slots=True)
class AuthoredWorldPackBuild:
    archive: bytes
    archive_sha256: str
    template: JsonObject
    report: JsonObject


@dataclass(frozen=True, slots=True)
class _AliasRecord:
    alias: str
    kind: str
    value: UUID
    path: str
    entity_kind: str | None = None


class AuthoredWorldPackBuilder:
    """Expand explicit aliases and adapters, then invoke portable-pack verification."""

    def __init__(self, authoring: WorldPackAuthoringApplication | None = None):
        self._authoring = authoring or WorldPackAuthoringApplication()

    def build(self, document: Mapping[str, Any]) -> AuthoredWorldPackBuild:
        if not isinstance(document, Mapping):
            raise AuthoredWorldPackBuildError("输入必须是 JSON 对象。")
        if set(document) != {"manifest", "template"}:
            raise AuthoredWorldPackBuildError("输入只接受 manifest 与 template 两个顶层字段。")
        manifest = document.get("manifest")
        template_value = document.get("template")
        if not isinstance(manifest, Mapping):
            raise AuthoredWorldPackBuildError("manifest 必须是 JSON 对象。", path="manifest")
        if not isinstance(template_value, Mapping):
            raise AuthoredWorldPackBuildError("template 必须是 JSON 对象。", path="template")
        pack_id = manifest.get("pack_id")
        if not isinstance(pack_id, str) or not _PACK_ID_PATTERN.fullmatch(pack_id):
            raise AuthoredWorldPackBuildError("manifest.pack_id 必须是有效标识。", path="manifest.pack_id")

        template = deepcopy(dict(template_value))
        compiler_reports = self._compile_template_scenarios(template, pack_id)
        preview = self._authoring.preview(manifest=manifest, template=template)
        report = dict(preview.report)
        report["authored_ids"] = {"scenarios": compiler_reports}
        return AuthoredWorldPackBuild(
            archive=preview.archive,
            archive_sha256=preview.archive_sha256,
            template=preview.verified.template,
            report=report,
        )

    @staticmethod
    def _compile_template_scenarios(template: JsonObject, pack_id: str) -> list[JsonObject]:
        reports: list[JsonObject] = []
        if "scenario" in template and template["scenario"] is not None:
            compiler = _ScenarioCompiler(pack_id, "default")
            template["scenario"] = compiler.compile(
                template["scenario"], opening_id="default", path="template.scenario",
            )
            reports.append(compiler.report())
        if "story_openings" not in template:
            return reports
        openings = template["story_openings"]
        if not isinstance(openings, list):
            raise AuthoredWorldPackBuildError("story_openings 必须是 JSON 数组。", path="template.story_openings")
        compiled = []
        for index, raw in enumerate(openings):
            path = f"template.story_openings[{index}]"
            if not isinstance(raw, Mapping):
                raise AuthoredWorldPackBuildError("介入点必须是 JSON 对象。", path=path)
            opening = deepcopy(dict(raw))
            opening_id = opening.get("opening_id")
            if "scenario" in opening and opening["scenario"] is not None:
                if not isinstance(opening_id, str) or not _OPENING_ID_PATTERN.fullmatch(opening_id):
                    raise AuthoredWorldPackBuildError("含场景别名的介入点需要有效 opening_id。", path=f"{path}.opening_id")
                compiler = _ScenarioCompiler(pack_id, opening_id)
                opening["scenario"] = compiler.compile(
                    opening["scenario"], opening_id=opening_id, path=f"{path}.scenario",
                )
                reports.append(compiler.report())
            compiled.append(opening)
        template["story_openings"] = compiled
        return reports


class _ScenarioCompiler:
    def __init__(self, pack_id: str, opening_id: str):
        self.pack_id = pack_id
        self.opening_id = opening_id
        self.entity_aliases: dict[str, _AliasRecord] = {}
        self.route_aliases: dict[str, _AliasRecord] = {}
        self._all_aliases: dict[str, _AliasRecord] = {}
        self._generated_ids: dict[UUID, _AliasRecord] = {}

    def compile(self, raw_scenario: object, *, opening_id: str, path: str) -> JsonObject:
        if not isinstance(raw_scenario, Mapping):
            raise AuthoredWorldPackBuildError("scenario 必须是 JSON 对象。", path=path)
        scenario = deepcopy(dict(raw_scenario))
        entities_value = scenario.get("entities", [])
        alias_declarations = isinstance(entities_value, list)
        if alias_declarations:
            entity_rows = self._entity_rows(entities_value, path)
            raw_entity_map: dict[str, dict[str, Any]] = {}
        elif isinstance(entities_value, Mapping):
            entity_rows = []
            raw_entity_map = {}
            for key, value in entities_value.items():
                if not isinstance(value, Mapping):
                    raise AuthoredWorldPackBuildError("实体记录必须是 JSON 对象。", path=f"{path}.entities.{key}")
                raw_entity_map[str(key)] = deepcopy(dict(value))
        else:
            raise AuthoredWorldPackBuildError("entities 必须是别名实体数组或 UUID 到实体的对象。", path=f"{path}.entities")

        cards = scenario.get("character_cards", [])
        if not isinstance(cards, list):
            raise AuthoredWorldPackBuildError("character_cards 必须是 JSON 数组。", path=f"{path}.character_cards")
        arrivals = scenario.get("story_arrivals", [])
        if not isinstance(arrivals, list):
            raise AuthoredWorldPackBuildError("story_arrivals 必须是 JSON 数组。", path=f"{path}.story_arrivals")

        for index, row in entity_rows:
            self._register_entity(row["alias"], row.get("kind"), f"{path}.entities[{index}].alias")
        arrival_actor_paths: dict[int, tuple[str, dict[str, Any]]] = {}
        for index, arrival in enumerate(arrivals):
            actor = arrival.get("actor") if isinstance(arrival, Mapping) else None
            if isinstance(actor, Mapping) and "alias" in actor:
                alias = actor.get("alias")
                self._register_entity(alias, actor.get("kind"), f"{path}.story_arrivals[{index}].actor.alias")
                if "id" in actor:
                    raise AuthoredWorldPackBuildError("带 alias 的到场角色不能同时提供 id。", path=f"{path}.story_arrivals[{index}].actor.id")
                arrival_actor_paths[index] = (str(alias), deepcopy(dict(actor)))
        for index, card in enumerate(cards):
            if not isinstance(card, Mapping):
                raise AuthoredWorldPackBuildError("角色卡映射必须是 JSON 对象。", path=f"{path}.character_cards[{index}]")
            alias = card.get("alias")
            self._register_entity(alias, "character", f"{path}.character_cards[{index}].alias")
            if "id" in card:
                raise AuthoredWorldPackBuildError("角色卡映射不能提供 id；请使用 alias。", path=f"{path}.character_cards[{index}].id")

        if alias_declarations:
            for index, row in enumerate(entity_rows):
                if "id" in row:
                    raise AuthoredWorldPackBuildError("带 alias 的实体不能同时提供 id。", path=f"{path}.entities[{index}].id")

        source_entities = entity_rows if alias_declarations else list(enumerate(raw_entity_map.values()))
        for index, entity in source_entities:
            self._register_place_routes(
                entity, path=f"{path}.entities[{index}]", require_alias=alias_declarations,
            )
        for index, (_, actor) in arrival_actor_paths.items():
            self._register_place_routes(actor, path=f"{path}.story_arrivals[{index}].actor", require_alias=False)
        if not alias_declarations:
            for index, entity in enumerate(raw_entity_map.values()):
                if "alias" in entity:
                    raise AuthoredWorldPackBuildError(
                        "实体 alias 只能用于 entities 数组；UUID 字典保持原样。",
                        path=f"{path}.entities[{index}].alias",
                    )

        compiled_entities: dict[str, Any] = {}
        if alias_declarations:
            for index, row in entity_rows:
                alias = row["alias"]
                record = self.entity_aliases[alias]
                body = {key: value for key, value in row.items() if key != "alias"}
                transformed = self._transform(body, path=f"{path}.entities[{index}]", owner_entity_id=str(record.value))
                transformed["id"] = str(record.value)
                compiled_entities[str(record.value)] = transformed
        else:
            for index, (entity_id, entity) in enumerate(raw_entity_map.items()):
                compiled_entities[entity_id] = self._transform(
                    entity, path=f"{path}.entities[{index}]", owner_entity_id=entity_id,
                )

        card_reports = []
        for index, card in enumerate(cards):
            mapped, report = self._map_character_card(card, path=f"{path}.character_cards[{index}]")
            record = self.entity_aliases[str(card["alias"])]
            if str(record.value) in compiled_entities:
                raise AuthoredWorldPackBuildError("角色卡 alias 与现有实体 UUID 冲突。", path=f"{path}.character_cards[{index}].alias")
            mapped["id"] = str(record.value)
            compiled_entities[str(record.value)] = mapped
            card_reports.append({
                "alias": record.alias,
                "card_version": report["card_version"],
                "mapped_fields": report["mapped_fields"],
                "unmapped_fields": report["unmapped_fields"],
            })

        scenario["entities"] = compiled_entities
        if arrivals:
            compiled_arrivals = []
            for index, raw in enumerate(arrivals):
                if not isinstance(raw, Mapping):
                    raise AuthoredWorldPackBuildError("到场记录必须是 JSON 对象。", path=f"{path}.story_arrivals[{index}]")
                arrival = deepcopy(dict(raw))
                actor_entry = arrival_actor_paths.get(index)
                if actor_entry:
                    alias, actor = actor_entry
                    record = self.entity_aliases[alias]
                    actor.pop("alias", None)
                    actor = self._transform(actor, path=f"{path}.story_arrivals[{index}].actor", owner_entity_id=str(record.value))
                    actor["id"] = str(record.value)
                    arrival["actor"] = actor
                    arrival = self._transform(arrival, path=f"{path}.story_arrivals[{index}]", skip_actor=True)
                else:
                    arrival = self._transform(arrival, path=f"{path}.story_arrivals[{index}]")
                compiled_arrivals.append(arrival)
            scenario["story_arrivals"] = compiled_arrivals
        else:
            scenario.pop("story_arrivals", None)

        for field in _DSL_FIELDS:
            scenario.pop(field, None)
        for key, value in list(scenario.items()):
            if key not in {"entities", "story_arrivals"}:
                scenario[key] = self._transform(value, path=f"{path}.{key}", field_name=key)

        world_book_summary = self._map_world_book(scenario, raw_scenario, opening_id, path)
        entity_aliases = [record for record in self._all_aliases.values() if record.kind == "entity"]
        route_aliases = [record for record in self._all_aliases.values() if record.kind == "route"]
        self._stats = {
            "opening_id": self.opening_id,
            "entity_alias_count": len(entity_aliases),
            "route_alias_count": len(route_aliases),
            "character_cards": card_reports,
            "world_book": world_book_summary,
        }
        return scenario

    def _entity_rows(self, value: list[Any], path: str) -> list[tuple[int, dict[str, Any]]]:
        rows: list[tuple[int, dict[str, Any]]] = []
        for index, item in enumerate(value):
            row_path = f"{path}.entities[{index}]"
            if not isinstance(item, Mapping):
                raise AuthoredWorldPackBuildError("实体声明必须是 JSON 对象。", path=row_path)
            row = deepcopy(dict(item))
            alias = row.get("alias")
            if not isinstance(alias, str):
                raise AuthoredWorldPackBuildError("实体声明必须提供 alias。", path=f"{row_path}.alias")
            rows.append((index, row))
        return rows

    def _register_entity(self, alias: object, entity_kind: object, path: str) -> _AliasRecord:
        if not isinstance(alias, str) or not _ALIAS_PATTERN.fullmatch(alias):
            raise AuthoredWorldPackBuildError("alias 必须以字母开头，只能包含字母、数字、点、下划线或连字符。", path=path)
        if not isinstance(entity_kind, str):
            raise AuthoredWorldPackBuildError("实体 alias 需要同时提供 kind。", path=path)
        return self._register(alias, "entity", path, entity_kind=entity_kind)

    def _register_place_routes(self, entity: Mapping[str, Any], *, path: str, require_alias: bool) -> None:
        components = entity.get("components")
        place = components.get("place") if isinstance(components, Mapping) else None
        if not isinstance(place, Mapping) or "routes" not in place:
            return
        routes = place["routes"]
        if not isinstance(routes, list):
            raise AuthoredWorldPackBuildError("地点 routes 必须是 JSON 数组。", path=f"{path}.components.place.routes")
        declares_route_alias = require_alias or any(
            isinstance(route, Mapping) and "alias" in route for route in routes
        )
        if declares_route_alias and entity.get("kind") not in {"place", "location"}:
            raise AuthoredWorldPackBuildError(
                "路线 alias 必须声明在地点实体上。", path=f"{path}.components.place.routes",
            )
        for index, route in enumerate(routes):
            route_path = f"{path}.components.place.routes[{index}]"
            if not isinstance(route, Mapping):
                raise AuthoredWorldPackBuildError("路线声明必须是 JSON 对象。", path=route_path)
            if "alias" not in route:
                if require_alias:
                    raise AuthoredWorldPackBuildError("别名实体模式中的每条路线都需要 alias。", path=f"{route_path}.alias")
                continue
            if "route_id" in route:
                raise AuthoredWorldPackBuildError("带 alias 的路线不能同时提供 route_id。", path=f"{route_path}.route_id")
            self._register(route.get("alias"), "route", f"{route_path}.alias")

    def _register(self, alias: str, kind: str, path: str, *, entity_kind: str | None = None) -> _AliasRecord:
        if not isinstance(alias, str) or not _ALIAS_PATTERN.fullmatch(alias):
            raise AuthoredWorldPackBuildError("alias 必须以字母开头，只能包含字母、数字、点、下划线或连字符。", path=path)
        if alias in self._all_aliases:
            prior = self._all_aliases[alias]
            raise AuthoredWorldPackBuildError(
                f"alias {alias!r} 与 {prior.path} 的 {prior.kind} 声明冲突。", path=path,
            )
        value = uuid5(NAMESPACE_URL, f"world-engine-authored:{self.pack_id}:{self.opening_id}:{alias}")
        if value in self._generated_ids:
            prior = self._generated_ids[value]
            raise AuthoredWorldPackBuildError(f"alias 产生 UUID 冲突：{prior.alias!r} 与 {alias!r}。", path=path)
        record = _AliasRecord(alias, kind, value, path, entity_kind)
        self._all_aliases[alias] = record
        self._generated_ids[value] = record
        (self.entity_aliases if kind == "entity" else self.route_aliases)[alias] = record
        return record

    def _map_character_card(self, card: Mapping[str, Any], *, path: str) -> tuple[dict[str, Any], dict[str, Any]]:
        location = card.get("location_id")
        location_id = self._resolve_card_location(location, path=f"{path}.location_id")
        source = card.get("card")
        if isinstance(source, str):
            card_json = source
        elif isinstance(source, Mapping):
            try:
                card_json = json.dumps(source, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
            except (TypeError, ValueError, UnicodeError) as error:
                raise AuthoredWorldPackBuildError("角色卡不能编码为有限 JSON。", path=f"{path}.card") from error
        else:
            raise AuthoredWorldPackBuildError("角色卡需要 card JSON 对象或文本。", path=f"{path}.card")
        try:
            draft, report = character_card_draft(card_json, location_id)
        except InvalidEvent as error:
            raise AuthoredWorldPackBuildError(str(error), path=f"{path}.card") from error
        components: dict[str, Any] = {
            "personality": {"values": draft["values"], "traits": draft["traits"]},
        }
        if draft["description"]:
            components["public_description"] = draft["description"]
        if draft["public_role"]:
            components["public_role"] = draft["public_role"]
        return {
            "kind": "character",
            "name": draft["name"],
            "location_id": str(location_id),
            "status": "active",
            "components": components,
        }, report

    def _resolve_card_location(self, value: object, *, path: str) -> UUID:
        if isinstance(value, Mapping) and set(value) == {"$entity"}:
            resolved = self._resolve_reference(value, field_name="location_id", path=path)
        elif isinstance(value, str):
            try:
                resolved = UUID(value)
            except ValueError as error:
                raise AuthoredWorldPackBuildError("location_id 必须是实体别名引用或 UUID。", path=path) from error
        else:
            raise AuthoredWorldPackBuildError("location_id 必须是实体别名引用或 UUID。", path=path)
        return UUID(str(resolved))

    def _map_world_book(self, scenario: dict[str, Any], raw_scenario: Mapping[str, Any], opening_id: str, path: str) -> dict[str, Any] | None:
        adapter = raw_scenario.get("world_book_adapter")
        if adapter is None:
            return None
        if not isinstance(adapter, Mapping):
            raise AuthoredWorldPackBuildError("world_book_adapter 必须是 JSON 对象。", path=f"{path}.world_book_adapter")
        source = adapter.get("source")
        if isinstance(source, str):
            book_json = source
        elif isinstance(source, Mapping):
            try:
                book_json = json.dumps(source, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
            except (TypeError, ValueError, UnicodeError) as error:
                raise AuthoredWorldPackBuildError("world book 不能编码为有限 JSON。", path=f"{path}.world_book_adapter.source") from error
        else:
            raise AuthoredWorldPackBuildError("world_book_adapter.source 必须是 JSON 对象或 JSON 文本。", path=f"{path}.world_book_adapter.source")
        visibility = adapter.get("visibility", "author_only")
        if visibility not in {"public", "author_only"}:
            raise AuthoredWorldPackBuildError("world book visibility 只能是 public 或 author_only。", path=f"{path}.world_book_adapter.visibility")
        try:
            entries, skipped, _source_receipt = load_world_book(book_json)
        except InvalidEvent as error:
            raise AuthoredWorldPackBuildError(str(error), path=f"{path}.world_book_adapter.source") from error
        materialized = []
        for row in entries:
            entry_alias = f"world-book:{row['source_key']}"
            entry_id = uuid5(NAMESPACE_URL, f"world-engine-authored:{self.pack_id}:{opening_id}:{entry_alias}")
            if entry_id in self._generated_ids:
                raise AuthoredWorldPackBuildError("world book entry ID collides with an authored alias.", path=f"{path}.world_book_adapter")
            materialized.append({**row, "entry_id": str(entry_id), "visibility": visibility})
        existing = scenario.get("world_book_entries", [])
        if not isinstance(existing, list):
            raise AuthoredWorldPackBuildError("world_book_entries 必须是 JSON 数组。", path=f"{path}.world_book_entries")
        scenario["world_book_entries"] = [*existing, *materialized]
        return {
            "mapped_entry_count": len(materialized),
            "skipped_entries": skipped,
            "visibility": visibility,
        }

    def _transform(
        self,
        value: Any,
        *,
        path: str,
        field_name: str | None = None,
        owner_entity_id: str | None = None,
        skip_actor: bool = False,
    ) -> Any:
        if isinstance(value, Mapping):
            if set(value) == {"$entity"} or set(value) == {"$route"}:
                return self._resolve_reference(value, field_name=field_name, path=path)
            if any(isinstance(key, str) and key.startswith("$") for key in value):
                raise AuthoredWorldPackBuildError("引用标记必须是仅含 $entity 或 $route 的对象。", path=path)
            result = {}
            for key, item in value.items():
                if skip_actor and key == "actor":
                    result[key] = item
                else:
                    result[key] = self._transform(
                        item, path=f"{path}.{key}", field_name=str(key), owner_entity_id=owner_entity_id,
                        skip_actor=skip_actor,
                    )
            return result
        if isinstance(value, list):
            if field_name == "routes" and path.endswith(".components.place.routes"):
                return self._transform_routes(value, path=path, owner_entity_id=owner_entity_id)
            return [self._transform(item, path=f"{path}[{index}]", field_name=field_name,
                                    owner_entity_id=owner_entity_id, skip_actor=skip_actor)
                    for index, item in enumerate(value)]
        return value

    def _transform_routes(self, routes: list[Any], *, path: str, owner_entity_id: str | None) -> list[Any]:
        result = []
        for index, route in enumerate(routes):
            route_path = f"{path}[{index}]"
            if not isinstance(route, Mapping) or "alias" not in route:
                result.append(self._transform(route, path=route_path, owner_entity_id=owner_entity_id))
                continue
            alias = route["alias"]
            record = self.route_aliases.get(alias) if isinstance(alias, str) else None
            if record is None:
                raise AuthoredWorldPackBuildError(f"未定义路线 alias {alias!r}。", path=f"{route_path}.alias")
            if owner_entity_id is None:
                raise AuthoredWorldPackBuildError("路线需要包含它的地点实体。", path=route_path)
            raw = {key: value for key, value in route.items() if key not in {"alias", "route_id"}}
            transformed = self._transform(raw, path=route_path, owner_entity_id=owner_entity_id)
            if "from_place_id" in route:
                containing_place = self._transform(
                    route["from_place_id"], path=f"{route_path}.from_place_id", field_name="from_place_id",
                    owner_entity_id=owner_entity_id,
                )
                try:
                    same_place = UUID(str(containing_place)) == UUID(str(owner_entity_id))
                except ValueError:
                    same_place = False
                if not same_place:
                    raise AuthoredWorldPackBuildError("路线 from_place_id 必须指向包含该路线的地点。", path=f"{route_path}.from_place_id")
            transformed["from_place_id"] = str(owner_entity_id)
            transformed["route_id"] = str(record.value)
            result.append(transformed)
        return result

    def _resolve_reference(self, marker: Mapping[str, Any], *, field_name: str | None, path: str) -> str:
        marker_kind, alias = next(iter(marker.items()))
        if not isinstance(alias, str):
            raise AuthoredWorldPackBuildError("引用 alias 必须是文本。", path=path)
        if marker_kind == "$route":
            if field_name != "route_id":
                raise AuthoredWorldPackBuildError("$route 只能用于 route_id 字段。", path=path)
            record = self.route_aliases.get(alias)
            if record is None:
                raise AuthoredWorldPackBuildError(f"未定义路线 alias {alias!r}。", path=path)
            return str(record.value)
        if field_name == "route_id":
            raise AuthoredWorldPackBuildError("route_id 必须引用路线 alias。", path=path)
        if not (field_name and (field_name in _PLACE_REFERENCE_FIELDS or field_name.endswith(("_id", "_ids")))):
            raise AuthoredWorldPackBuildError("$entity 只能用于实体 ID 字段。", path=path)
        record = self.entity_aliases.get(alias)
        if record is None:
            raise AuthoredWorldPackBuildError(f"未定义实体 alias {alias!r}。", path=path)
        if field_name in _PLACE_REFERENCE_FIELDS and record.entity_kind not in {"place", "location"}:
            raise AuthoredWorldPackBuildError(f"{field_name} 要求地点 alias，{alias!r} 的 kind 是 {record.entity_kind!r}。", path=path)
        if field_name in _CHARACTER_REFERENCE_FIELDS and record.entity_kind not in {"character", "person"}:
            raise AuthoredWorldPackBuildError(f"{field_name} 要求人物 alias，{alias!r} 的 kind 是 {record.entity_kind!r}。", path=path)
        return str(record.value)

    def report(self) -> JsonObject:
        aliases = [
            {"alias": record.alias, "kind": record.kind, "id": str(record.value)}
            for record in self._all_aliases.values()
        ]
        return {**self._stats, "aliases": aliases}


def _load_unique_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON property {key!r}")
        result[key] = value
    return result


def _reject_nonfinite_json(value: str) -> None:
    raise ValueError(f"non-finite JSON number {value} is not allowed")


def loads_authored_world_pack(source: str) -> JsonObject:
    """Parse a source document while rejecting duplicate keys and non-finite numbers."""
    try:
        parsed = json.loads(
            source.removeprefix("\ufeff"),
            object_pairs_hook=_load_unique_json_object,
            parse_constant=_reject_nonfinite_json,
        )
    except (AttributeError, TypeError, ValueError, UnicodeError, RecursionError) as error:
        raise AuthoredWorldPackBuildError(f"来源文件不是有效 JSON：{error}") from error
    if not isinstance(parsed, dict):
        raise AuthoredWorldPackBuildError("来源文件根节点必须是 JSON 对象。")
    return parsed


def minimal_authored_world_pack() -> JsonObject:
    """Return a small complete example suitable for CLI output and tests."""
    return {
        "manifest": {
            "schema_version": "1.0",
            "pack_id": "river-road-example",
            "version": "0.1.0",
            "engine_range": "*",
            "title": "河路小世界",
            "license": "UNLICENSED",
        },
        "template": {
            "story_openings": [{
                "opening_id": "first-step",
                "title": "渡口初见",
                "position": "从河岸渡口开始。",
                "known_history": [],
                "scenario": {
                    "player_id": {"$entity": "traveler"},
                    "entities": [
                        {
                            "alias": "traveler",
                            "kind": "character",
                            "name": "旅人",
                            "location_id": {"$entity": "ferry"},
                            "components": {
                                "health": {"current": 100, "maximum": 100},
                                "capabilities": {"grants": ["world.travel"]},
                                "known_places": [{"$entity": "north-bank"}],
                            },
                        },
                        {
                            "alias": "ferry",
                            "kind": "place",
                            "name": "河岸渡口",
                            "components": {"place": {
                                "place_type": "settlement",
                                "routes": [{
                                    "alias": "north-bank-road",
                                    "to_place_id": {"$entity": "north-bank"},
                                    "travel_ticks": 3600,
                                }],
                            }},
                        },
                        {
                            "alias": "north-bank",
                            "kind": "place",
                            "name": "北岸小路",
                            "components": {"place": {"place_type": "wilderness"}},
                        },
                    ],
                },
            }],
        },
    }
