"""Build the playable Gensokyo world independently of recovered assets."""

from uuid import UUID

from world_engine.application.world_packs import WorldPackManifest, write_world_pack
from world_engine.domain.challenges import ChallengeDefinition
from world_engine.domain.components import GoalRecord, KnowledgeRecord, PlaceComponent, ProvenanceReference, RelationRecord
from world_engine.domain.models import StoryArrival, WorldEntity, WorldState
from world_engine.domain.npc_plan_review import build_npc_plan_review_schedule
from world_engine.domain.story_nodes import StoryFollowup, StoryNode
from world_engine.domain.world_books import WorldBookCitation, WorldBookEntry, WorldBookProvenance

from .gensokyo_cast_early import EARLY_CAST_FACTS, EARLY_CAST_RELATIONS, EARLY_CAST_SOURCES
from .gensokyo_cast_habitats import HABITATS
from .gensokyo_cast_middle import MIDDLE_CAST_FACTS, MIDDLE_CAST_SOURCES
from .gensokyo_cast_personae_late import LATE_CAST_FACTS, LATE_CAST_PRIVATE_FACTS
from .gensokyo_cast_relations_late import LATER_CAST_RELATIONS
from .gensokyo_chronology import CHAPTERS, CONTINUATION_DAYS, HISTORY_WORKS, Chapter, incident_nodes
from .gensokyo_disciplines import local_play, skill_levels
from .gensokyo_original_profiles import GEOGRAPHY, PROFILES, source_id
from .gensokyo_reference import (
    _PLACE_SOURCE_ORDER, _PROFILE_SOURCE_ORDER, _id, build_geography_index, build_reference_entries,
    build_source_index,
)
from .gensokyo_world_lore import LORE_ENTRIES, LORE_SOURCES
from .gensokyo_world_map import connect_world_map, register_passage
from .gensokyo_village_life import RESOURCE_LABELS, add_local_economy


PACK_ID = "author.gensokyo-world"
PACK_VERSION = "1.1.0"
PACK_TITLE = "东方幻想乡·异变年代"
_HOUR = 3600

_ENTRY_WINDOWS = {
    "yukari": 7, "alice": 7, "youmu": 7, "yuyuko": 7,
    "eirin": 8, "kaguya": 8, "tewi": 8,
}
_PRIVATE_HOMES = {"mansion", "mansion-library", "eientei"}
_REALM_PERMITS = {
    "outside-world": ("barrier",),
    "netherworld": ("netherworld",),
    "saigyouji-residence": ("netherworld",),
    "heaven": ("heaven",),
    "shining-needle-castle": ("sky",),
    "higan": ("sanzu",),
    "hell": ("sanzu", "hell"),
    "hell-checkpoint": ("sanzu", "hell"),
    "animal-realm": ("sanzu", "hell", "animal_realm"),
    "primate-garden": ("sanzu", "hell", "animal_realm"),
    "makai": ("makai",),
    "dream-world": ("dream",),
    "lunar-capital": ("dream", "lunar"),
}
_GROUPS = {
    "scarlet-household": ("remilia", "sakuya", "meiling"),
    "prismriver-ensemble": ("lunasa", "merlin", "lyrica"),
    "mountain-tengu": ("aya", "momiji", "megumu"),
    "mountain-kappa": ("nitori",),
    "moriya-faith": ("kanako", "suwako", "sanae"),
    "myouren-community": ("byakuren", "ichirin", "murasa", "shou", "kyouko"),
    "eagle-ravi": ("ringo", "seiran"),
    "kiketsu": ("yachie",),
    "keiga": ("saki",),
    "mining-operation": ("megumu", "momoyo"),
}
_LEADERS = {"scarlet-household", "myouren-community", "kiketsu", "keiga", "mining-operation"}
_NIGHT_ACTIVE = {"yukari", "remilia", "flandre", "wriggle", "mystia", "rumia", "kagerou"}
_STUDY_OUTINGS = {"marisa", "alice", "patchouli", "eirin", "nitori", "misumaru"}
_SOCIAL_OUTINGS = {"suika", "lunasa", "merlin", "lyrica", "aunn", "byakuren"}
_GEOGRAPHY_WORKS = (
    {key: number for number, rows in GEOGRAPHY.items() for key, *_ in rows}
    | _PLACE_SOURCE_ORDER | {"mayohiga": 7, "netherworld": 7, "saigyouji-residence": 7, "eientei": 8, "sanzu-river": 9}
)
_REFERENCE_WORKS = _GEOGRAPHY_WORKS | HISTORY_WORKS | {
    "youkai-powers": 7, "ice-spirit-wording": 9, "mansion-lake-inference": 14, "tech-and-market": 18,
}
_PROFILE_WORKS = {key: work for work, profiles in PROFILES.items() for key, *_ in profiles}
_CAST_FACTS = EARLY_CAST_FACTS | MIDDLE_CAST_FACTS | LATE_CAST_FACTS
_STANDING_RELATIONSHIP_GOALS = {
    "sakuya": ("remilia", "与蕾米莉亚核对访客和馆务安排"),
    "remilia": ("sakuya", "听取咲夜的馆务意见，商定近期待客安排"),
    "alice": ("marisa", "与魔理沙商议互相借阅的魔法书和归还条件"),
    "youmu": ("yuyuko", "请幽幽子复核白玉楼的庭院来访安排"),
    "yuyuko": ("youmu", "听取妖梦对庭门和值守的意见"),
    "eirin": ("reisen", "和铃仙约定下一次核对竹林来路和药箱的时段"),
    "reisen": ("eirin", "向永琳询问本次巡查所见需要怎样处理"),
    "sanae": ("kanako", "和神奈子商议下次参拜日怎样接待山下来客"),
    "kanako": ("sanae", "向早苗了解人里访客对守矢神社的顾虑"),
    "byakuren": ("shou", "与寅丸星商谈寺院供物和来客安排"),
    "shou": ("byakuren", "与白莲核对寺院实际收到的供物及所需帮助"),
    "doremy": ("sagume", "与探女商量哪些梦境通道应暂缓使用"),
    "okina": ("satono", "召集里乃讨论接下来需要巡查的门"),
    "satono": ("okina", "向隐岐奈确认近期外出所收集的见闻"),
    "megumu": ("chimata", "与千亦商议市场如何继续开设"),
    "chimata": ("megumu", "与饭纲丸龙澄清下次市场的供货条件"),
}


def _chapter_continuations(
    chapter: Chapter,
    incident_id: UUID,
    history: dict[str, WorldBookEntry],
    cast_entries: dict[str, WorldBookEntry],
    availability: dict[str, int],
    entities: dict[UUID, WorldEntity],
    atlas_work: dict[UUID, int],
    future_documents: dict[int, list[UUID]],
    player_id: UUID,
) -> tuple[list[StoryFollowup], list[StoryArrival], list[ChallengeDefinition]]:
    followups = []
    arrivals = []
    reports = []
    playable_chapters = {item.key: item for item in CHAPTERS}
    chapter_key = chapter.key
    known_entities = dict(entities)
    for next_key, work in HISTORY_WORKS.items():
        if work <= chapter.work:
            continue
        trigger = _id(f"world:continuation:{chapter.key}:{next_key}")
        active_atlas = {ident: item for ident, item in entities.items() if atlas_work.get(ident, 6) <= work}
        pending = _stage_cast_wave(work, trigger, cast_entries, availability, active_atlas, known_entities)
        opens = [ident for ident, available in atlas_work.items() if available == work]
        evidence = history[next_key]
        followups.append(StoryFollowup(
            followup_id=trigger, after_id=incident_id,
            delay_ticks=CONTINUATION_DAYS.get(chapter_key, 180) * 24 * _HOUR,
            node=StoryNode.model_validate({
                "title": evidence.title,
                "visibility": "public",
                "references": evidence.provenance.citations,
                "participant_ids": [arrival.actor.id for arrival in pending],
                "opens_place_ids": [ident for ident in opens if entities[ident].kind == "place"],
                "opens_organization_ids": [ident for ident in opens if entities[ident].kind == "organization"],
                "reveals_entry_ids": future_documents.get(work, []),
                "enrollments": [
                    {"organization_id": _id(f"organization:{group}"), "actor_id": actor_id(member),
                     "leader": index == 0 and group in _LEADERS}
                    for group, members in _GROUPS.items() for index, member in enumerate(members)
                    if max(_GEOGRAPHY_WORKS.get(group, 6), availability.get(member, 6)) == work
                ],
                "fallback": {
                    "key": "new-period", "effect": "occurred", "summary": evidence.content,
                },
            }),
        ))
        arrivals.extend(pending)
        incident_id = trigger
        chapter_key = next_key
        if (later := playable_chapters.get(next_key)) is not None:
            scheduled, afters = incident_nodes(later, evidence, player_id, actor_id(later.contact))
            followups.extend([
                StoryFollowup(
                    followup_id=scheduled.id, after_id=trigger, delay_ticks=scheduled.due_tick,
                    node=StoryNode.model_validate(scheduled.payload),
                ),
                *afters,
            ])
            reports.append(_chapter_report(
                later, evidence, entities, cast_entries[later.contact].title, scheduled.id, opens_after=trigger,
            ))
            incident_id = scheduled.id
    return followups, arrivals, reports


def _chapter_report(
    chapter: Chapter, evidence: WorldBookEntry, entities: dict[UUID, WorldEntity], contact_name: str,
    deadline: UUID, *, opens_after: UUID | None = None,
) -> ChallengeDefinition:
    meeting = _id(f"place:{chapter.meeting}")
    return ChallengeDefinition(
        challenge_id=_id(f"world:incident:{chapter.key}:report"),
        venue_id=meeting,
        title=f"{evidence.title}线索登记",
        skill_id="gensokyo.observation",
        skill_label="现场记录",
        pass_level="1",
        opens_after_story_id=opens_after,
        closes_after_story_id=deadline,
        description=f"在首轮调查结算前，于{entities[meeting].name}提交线索并联系{contact_name}。",
    )


def _stage_cast_wave(
    work: int,
    trigger: UUID,
    cast_entries: dict[str, WorldBookEntry],
    availability: dict[str, int],
    entities: dict[UUID, WorldEntity],
    known_entities: dict[UUID, WorldEntity],
) -> list[StoryArrival]:
    pending = []
    for index, (key, reference) in enumerate(cast_entries.items()):
        if availability.get(key, 6) != work:
            continue
        facts, traits = _cast_reference(reference, work)
        pending.append((key, *_make_actor(key, facts, traits, entities, index, work)))
    known_entities.update((actor.id, actor) for _, actor, _, _ in pending)
    relations, _ = _seed_relationships(known_entities, work)
    by_arriving_actor: dict[UUID, list[RelationRecord]] = {actor.id: [] for _, actor, _, _ in pending}
    for relation in relations:
        owner = relation.from_id if relation.from_id in by_arriving_actor else relation.to_id
        if owner in by_arriving_actor:
            by_arriving_actor[owner].append(relation)
    return [
        StoryArrival(
            trigger_id=trigger, actor=actor, goals=(goal, *_standing_goals(key, known_entities)),
            relations=tuple(by_arriving_actor[actor.id]),
            knowledge=held, review_delay_ticks=_HOUR + index * 900,
        )
        for index, (key, actor, goal, held) in enumerate(pending)
    ]


def actor_id(key: str) -> UUID:
    return _id(f"world:actor:{key}")


def world_source_index() -> dict[str, dict[str, str]]:
    return build_source_index() | EARLY_CAST_SOURCES | MIDDLE_CAST_SOURCES | LORE_SOURCES | {
        "legacy.th08.yukari-card": {
            "work": "旧《Touhou TH08 Sandbox》八云紫角色卡",
            "host": "用户授权的旧项目恢复副本",
            "file": "recovered/data/characters/char_yukari_th07.json",
        },
    }


def world_geography_index() -> list[dict]:
    return [
        row | {"available_from_work": _GEOGRAPHY_WORKS.get(row["ref"], 6)}
        for row in [*build_geography_index(), {
            "ref": "mayohiga", "kind": "settlement", "parent": None,
            "source_document": "zun.th07.sakuya-dialogue",
        }]
    ]


def _build_atlas(work: int, entries: list[WorldBookEntry]) -> tuple[dict[UUID, WorldEntity], dict[UUID, int]]:
    geography = world_geography_index()
    references = {entry.source_key: entry for entry in entries}
    entities = {}
    availability = {}
    for row in geography:
        key, kind, parent, opens = (row[field] for field in ("ref", "kind", "parent", "available_from_work"))
        category = "organization" if kind == "organization" else "place"
        ident = _id(f"{category}:{key}")
        parent_id = _id(f"place:{parent}") if parent else None
        components = {}
        if opens > work:
            components["hidden"] = True
        if category == "place":
            components["place"] = PlaceComponent(
                place_type=kind, parent_id=parent_id,
                access_policy="private" if key in _PRIVATE_HOMES else "open",
            ).model_dump(mode="json")
        entities[ident] = WorldEntity(
            id=ident, kind=category, name=references[key].title,
            status="active" if opens <= work else "closed",
            location_id=parent_id if category == "organization" else None, components=components,
        )
        availability[ident] = opens
    shore = _id("place:mansion-lakeshore")
    entities[shore] = WorldEntity(
        id=shore, kind="place", name="红魔馆外湖岸", components={
            "place": PlaceComponent(place_type="site").model_dump(mode="json"),
            "public_description": "湖边可以望见红魔馆。进馆须先在门外办理来访许可。",
        },
    )
    connect_world_map(entities)
    return entities, availability


def _timeline_book(work: int, entries: list[WorldBookEntry], availability: dict[str, int]) -> tuple[
    list[WorldBookEntry], dict[int, list[UUID]],
]:
    book = []
    future: dict[int, list[UUID]] = {}
    for entry in entries:
        opens = availability.get(entry.source_key, 6)
        if entry.visibility == "public" and opens > work:
            future.setdefault(opens, []).append(entry.entry_id)
            entry = entry.model_copy(update={"visibility": "author_only"})
        book.append(entry)
    return book, future


def _contact_leads(
    atlas: dict[UUID, WorldEntity], atlas_work: dict[UUID, int],
    cast: list[WorldBookEntry], availability: dict[str, int],
) -> tuple[list[WorldBookEntry], dict[str, int]]:
    entries = []
    timeline = {}
    for person in cast:
        home, alternate, _ = HABITATS[person.source_key]
        opens = availability.get(person.source_key, 6)
        stops = []
        for key in (home, alternate):
            if key is not None and (ident := _id(f"place:{key}")) in atlas and atlas_work.get(ident, 6) <= opens:
                stops.append(atlas[ident].name)
        if not stops:
            raise ValueError(f"No active contact location for {person.source_key}")
        ref = f"visit-{person.source_key}"
        timeline[ref] = opens
        entries.append(WorldBookEntry(
            entry_id=_id(f"world:lore:{ref}"), source_key=ref, title=f"拜访{person.title}",
            keywords=tuple(dict.fromkeys((f"{person.title}在哪", f"找{person.title}", *person.keywords))),
            content=f"想找{person.title}，可以先去{'、'.join(stops)}打听。实际位置取决于当前日程和最近行动；"
                    "地图显示通行要求，到了当地再确认对方是否在场。",
            always_on=False, visibility="public",
            provenance=WorldBookProvenance(category="other", status="game_configuration"),
        ))
    return entries, timeline


def world_lore(work: int) -> list[WorldBookEntry]:
    registry = world_source_index()
    entries = [
        WorldBookEntry(
            entry_id=_id(f"world:lore:{row.key}"), source_key=row.key, title=row.title,
            keywords=(row.title, *row.aliases), content=row.content, always_on=False, visibility="public",
            provenance=WorldBookProvenance(
                category=row.category, status=row.status,
                citations=(WorldBookCitation(
                    source_id=row.source_id, work=registry[row.source_id]["work"],
                    locator=row.locator, viewpoint=row.viewpoint,
                ),),
            ),
        )
        for row in LORE_ENTRIES if row.available_from <= work
    ]
    if work >= 7:
        entries.extend([
            WorldBookEntry(
                entry_id=_id("world:lore:mayohiga"), source_key="mayohiga", title="迷途之家",
                keywords=("迷途之家", "マヨヒガ", "橙出现在哪里"),
                content="《东方妖妖梦》第二面以迷途之家为标题，橙在这一段与自机相遇。这里的步行路线和访客时刻由世界包配置。",
                always_on=False, visibility="public",
                provenance=WorldBookProvenance(category="place", status="documented", citations=(
                    WorldBookCitation(
                        source_id="zun.th07.sakuya-dialogue", work=registry["zun.th07.sakuya-dialogue"]["work"],
                        locator="Stage 2 · 关卡标题与咲夜、橙的相遇",
                    ),
                )),
            ),
            WorldBookEntry(
                entry_id=_id("world:lore:old-card-yukari-venue"), source_key="old-card-yukari-venue",
                title="旧作演出参考·八云紫", keywords=("八云紫", "迷途之家"),
                content="旧游戏卡把八云紫的活动节点绑定到迷途之家。本包把该地点用作她可前往巡察的场地，原作住所仍按原作来源记录。",
                always_on=False, visibility="author_only",
                provenance=WorldBookProvenance(category="other", status="game_configuration", citations=(
                    WorldBookCitation(
                        source_id="legacy.th08.yukari-card", work=registry["legacy.th08.yukari-card"]["work"],
                        locator="char_yukari_th07.json · bind_node",
                    ),
                )),
            ),
        ])
    return entries


def _attach_organizations(entities: dict[UUID, WorldEntity], entries: list[WorldBookEntry]) -> None:
    for entry in entries:
        if entry.provenance.category != "organization":
            continue
        organization_id = _id(f"organization:{entry.source_key}")
        organization = entities.get(organization_id)
        if organization is None:
            continue
        registered = _GROUPS.get(entry.source_key, ())
        members = [actor_id(key) for key in registered if actor_id(key) in entities and organization.status == "active"]
        leader = actor_id(registered[0]) if members and entry.source_key in _LEADERS else None
        if leader not in members:
            leader = None
        entities[organization_id] = organization.model_copy(update={
            "components": organization.components | {"governance": {
                "members": [str(member) for member in members],
                "leader_id": str(leader) if leader else None,
                "charter": entry.content,
            }},
        })


def _cast_reference(facts: WorldBookEntry, work: int) -> tuple[WorldBookEntry, list[str]]:
    profile = _CAST_FACTS.get(facts.source_key)
    if profile is None or profile.document > work:
        return facts, [facts.content]
    source = profile.source or source_id(profile.document)
    citation = WorldBookCitation(source_id=source, work=world_source_index()[source]["work"], locator=profile.locator)
    traits = list(profile.traits)
    citations = [citation]
    content = "；".join(traits) + "。"
    if _PROFILE_WORKS.get(facts.source_key, work + 1) <= work:
        content = facts.content + content
        traits.insert(0, facts.content)
        citations.extend(item for item in facts.provenance.citations if item != citation)
    updated = facts.model_copy(update={
        "content": content,
        "provenance": WorldBookProvenance(category="person", status="documented", citations=tuple(citations)),
    })
    return updated, traits


def _seed_relationships(entities: dict[UUID, WorldEntity], work: int) -> tuple[list[RelationRecord], list[KnowledgeRecord]]:
    relations = []
    held = []
    for row in (*EARLY_CAST_RELATIONS, *LATER_CAST_RELATIONS):
        source, target = actor_id(row.subject), actor_id(row.target)
        if row.document > work or source not in entities or target not in entities:
            continue
        for first, second in ((source, target), (target, source)):
            label = row.description
            relations.append(RelationRecord(
                id=_id(f"world:relation:{first}:{second}"), from_id=first, to_id=second, label=label,
                dimensions={"familiarity": "12"},
            ))
            held.append(KnowledgeRecord(
                id=_id(f"world:relation-knowledge:{first}:{second}"), holder_id=first, subject_id=second,
                claim=label, kind="fact", confidence="1", visibility={"policy": "private", "subject_ids": [first]},
                provenance=[ProvenanceReference(
                    kind="pack", reference_id=f"{row.source or source_id(row.document)} · {row.locator}",
                )],
            ))
    return relations, held


def _make_actor(
    key: str, facts: WorldBookEntry, traits: list[str], entities: dict[UUID, WorldEntity], index: int, work: int,
) -> tuple[WorldEntity, GoalRecord, tuple[KnowledgeRecord, ...]]:
    home_key, errand_key, task = HABITATS[key]
    home = _id(f"place:{home_key}")
    errand = _id(f"place:{errand_key}") if errand_key else None
    if home not in entities:
        if errand not in entities:
            raise ValueError(f"No available scenario location for {key}: {home_key}, {errand_key}")
        home, home_key, errand = errand, errand_key, None
    if errand not in entities:
        errand = None
    grants = ["world.travel"]
    for place_key in (home_key, errand_key):
        if place_key in _PRIVATE_HOMES and "place.enter_private" not in grants:
            grants.append("place.enter_private")
        for site, permit in (("eientei", "gensokyo.eientei_guest"),
                             ("mansion", "gensokyo.mansion_guest"), ("mansion-library", "gensokyo.mansion_guest")):
            if place_key == site and permit not in grants:
                grants.append(permit)
        for name in _REALM_PERMITS.get(place_key, ()):
            permit = f"gensokyo.{name}_passage"
            if permit not in grants:
                grants.append(permit)

    role = actor_id(key)
    routines = _routines(key, home, errand, entities, index, grants)
    actor = WorldEntity(
        id=role, kind="character", name=facts.title, location_id=home,
        components={
            "personality": {"traits": traits, "locked_fields": ["traits"]},
            "capabilities": {"grants": grants},
            "skills": skill_levels(key),
            "known_places": [str(place) for place in dict.fromkeys([home, errand]) if place is not None],
            **routines,
        },
    )
    target = errand if errand != home else None
    goal = GoalRecord(
        id=_id(f"world:goal:{key}"), owner_id=role, title=task,
        description=f"当前地点是{entities[home].name}。"
                    + (f"如条件允许，前往{entities[target].name}处理这件事。" if target else "在当地寻找合适的处理机会。"),
        auto_complete=target is not None,
        conditions=[{"kind": "reach_location", "subject_id": role, "target_id": target}] if target else [],
    )
    knowledge = [KnowledgeRecord(
        id=_id(f"world:knowledge:{key}:self"), holder_id=role, subject_id=role, claim=facts.content,
        kind="fact", confidence="1", visibility={"policy": "private", "subject_ids": [role]},
        provenance=[ProvenanceReference(kind="pack", reference_id=f"{item.source_id} · {item.locator}")
                    for item in facts.provenance.citations],
    )]
    private = LATE_CAST_PRIVATE_FACTS.get(key)
    if private is not None and private.document <= work:
        knowledge.append(KnowledgeRecord(
            id=_id(f"world:knowledge:{key}:restricted"), holder_id=role, subject_id=role,
            claim="；".join(private.traits) + "。", kind="fact", confidence="1",
            visibility={"policy": "private", "subject_ids": [role]},
            provenance=[ProvenanceReference(
                kind="pack", reference_id=f"{private.source or source_id(private.document)} · {private.locator}",
            )],
        ))
    return actor, goal, tuple(knowledge)


def _routines(
    key: str, home: UUID, errand: UUID | None, entities: dict[UUID, WorldEntity], index: int, grants: list[str],
) -> dict:
    night_active = key in _NIGHT_ACTIVE
    rest = str(_id(f"world:routine:{key}:rest"))
    windows = [{
        "window_id": rest, "start_second": (7 if night_active else 18 + index % 2) * _HOUR,
        "duration_seconds": 5 * _HOUR, "activity": "rest", "place_id": str(home),
    }]
    result = {"daily_routine": {"windows": windows}}
    if errand is None or errand == home:
        return result

    def can_travel(start: UUID, destination: UUID) -> bool:
        return any(
            UUID(route["to_place_id"]) == destination and route["is_open"]
            and set(route["access_tags"]).issubset(grants)
            for route in entities[start].components["place"]["routes"]
        )

    if not (can_travel(home, errand) and can_travel(errand, home)):
        return result
    outing = str(_id(f"world:routine:{key}:outing"))
    activity = "study" if key in _STUDY_OUTINGS else "socialize" if key in _SOCIAL_OUTINGS else "work"
    start = (20 if night_active else 11 + index % 4) * _HOUR
    windows.append({
        "window_id": outing, "start_second": start, "duration_seconds": 2 * _HOUR,
        "activity": activity, "place_id": str(errand),
    })
    period = 3 + index % 5
    result["routine_mobility"] = {
        "enabled": True,
        "cadences": {outing: {"every_days": period, "phase_day": 1 + index % (period - 1)}},
        "alternatives": {outing: [str(home)]},
        "avoid_conditions": ["storm", "heavy_rain"],
    }
    return result


def _standing_goals(key: str, entities: dict[UUID, WorldEntity]) -> tuple[GoalRecord, ...]:
    if (relationship := _STANDING_RELATIONSHIP_GOALS.get(key)) is None:
        return ()
    partner, goal = relationship
    if actor_id(partner) not in entities:
        return ()
    return (GoalRecord(
        id=_id(f"world:social-goal:{key}"), owner_id=actor_id(key), title=goal, priority=55,
        description="见面可先沟通，若还需要讨论可提议另约时间。对方可按自己的日程和立场答复。",
    ),)


def build_opening(chapter: Chapter) -> dict:
    references = build_reference_entries()
    history = {
        entry.source_key: entry for entry in references if entry.source_key in HISTORY_WORKS
    }
    known_history = frozenset(key for key, work in HISTORY_WORKS.items() if work <= chapter.work)
    lore = world_lore(max(HISTORY_WORKS.values()))
    entities, atlas_work = _build_atlas(chapter.work, [*references, *lore])
    active_atlas = {ident: entity for ident, entity in entities.items() if entity.status == "active"}
    player_start = _id(f"place:{chapter.start}")
    initial_landmarks = [
        _id(f"place:{key}") for key in ("human-village", "shrine", "forest", "bamboo", "mansion-lakeshore",
                                        "youkai-mountain", "moriya-shrine")
    ]
    player = WorldEntity(
        id=_id("actor:resident"), name="异变调查来客", kind="character", location_id=player_start,
        components={
            "capabilities": {"grants": ["world.travel", "world.trade", *chapter.access]},
            "skills": skill_levels("player"),
            "known_places": list(dict.fromkeys([
                str(player_start), *(str(ident) for ident in initial_landmarks if ident in active_atlas),
            ])),
        },
    )
    entities[player.id] = player

    cast_entries = {
        entry.source_key: entry for entry in references if entry.provenance.category == "person"
        and entry.provenance.status == "documented"
    }
    if cast_entries.keys() != HABITATS.keys():
        raise ValueError(f"Cast and habitat coverage differ: {sorted(cast_entries.keys() ^ HABITATS.keys())}")
    availability = _PROFILE_SOURCE_ORDER | _PROFILE_WORKS | _ENTRY_WINDOWS | {
        key: fact.document for key, fact in EARLY_CAST_FACTS.items() if key in cast_entries
    }
    cast_documents = []
    goals = []
    knowledge = []
    for index, (key, facts) in enumerate(cast_entries.items()):
        available_from = availability.get(key, 6)
        facts, traits = _cast_reference(facts, max(available_from, chapter.work))
        cast_documents.append(facts)
        if available_from > chapter.work:
            continue
        actor, goal, held = _make_actor(key, facts, traits, active_atlas, index, chapter.work)
        entities[actor.id] = actor
        goals.append(goal)
        knowledge.extend(held)
    for key in cast_entries:
        if actor_id(key) in entities:
            goals.extend(_standing_goals(key, entities))

    static_documents = [entry for entry in references if entry.source_key not in cast_entries]
    visit_leads, lead_timeline = _contact_leads(entities, atlas_work, cast_documents, availability)
    book_timeline = _REFERENCE_WORKS | availability | lead_timeline | {row.key: row.available_from for row in LORE_ENTRIES}
    world_book, future_documents = _timeline_book(
        chapter.work, [*static_documents, *cast_documents, *visit_leads, *lore], book_timeline,
    )
    _attach_organizations(entities, world_book)
    relations, shared_history = _seed_relationships(entities, chapter.work)
    incident, followups = incident_nodes(chapter, history[chapter.key], player.id, actor_id(chapter.contact))
    continuation, arrivals, later_reports = _chapter_continuations(
        chapter, incident.id, history, cast_entries, availability, entities, atlas_work, future_documents, player.id,
    )
    challenges, contests = local_play(entities, actor_id("reimu"), actor_id("marisa"))
    access_challenges, permit_reactions = register_passage(player.id, entities)
    challenges.extend(access_challenges)
    meeting = _id(f"place:{chapter.meeting}")
    challenges.extend([
        _chapter_report(chapter, history[chapter.key], entities, cast_entries[chapter.contact].title, incident.id),
        *later_reports,
    ])
    resources = add_local_economy(entities, player.id)
    state = WorldState(
        player_id=player.id, entities=entities, goals=goals, resources=resources,
        knowledge=[*knowledge, *shared_history], relations=relations,
        world_book_entries=world_book, scheduled_events=[incident],
        story_followups=[*followups, *continuation], story_arrivals=arrivals,
        story_reactions=permit_reactions,
        challenges=challenges, contest_definitions=contests,
        metadata={
            "pack_id": PACK_ID, "pack_version": PACK_VERSION, "public_title": PACK_TITLE,
            "public_description": chapter.position + f" 如要介入首轮调查，可在{entities[meeting].name}登记线索并联系"
            f"{entities[actor_id(chapter.contact)].name}。路程、日程、来客资格和调查后果为世界包的模拟配置。",
            "reference_incident": chapter.key,
            "resource_catalog": RESOURCE_LABELS,
        },
    )
    reviews = [
        build_npc_plan_review_schedule(
            state, _id(f"world:reviews:{chapter.key}"), actor, _HOUR + index * 900,
        )
        for index, actor in enumerate(dict.fromkeys(goal.owner_id for goal in goals))
    ]
    state = WorldState.model_validate(state.model_dump(mode="python") | {
        "scheduled_events": [incident, *reviews],
    })
    past = [history[key].content for key in HISTORY_WORKS if key in known_history and key != chapter.key]
    return {
        "opening_id": chapter.key, "title": history[chapter.key].title,
        "position": chapter.position, "known_history": [*past[-10:], chapter.position],
        "scenario": state.model_dump(mode="json"),
    }


def build_archive() -> bytes:
    return write_world_pack(
        manifest=WorldPackManifest(pack_id=PACK_ID, version=PACK_VERSION, title=PACK_TITLE),
        template={"story_openings": [build_opening(chapter) for chapter in CHAPTERS]},
    )
