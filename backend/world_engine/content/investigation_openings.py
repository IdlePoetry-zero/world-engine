"""Human-authored opening investigation drafts for the 2.0 backgrounds.

The drafts keep authored evidence separate from mechanics: case routes use only
typed status/location facts and provenance roots, while the Chinese claims give
players an intelligible reason to pursue each independent lead.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from uuid import UUID

from world_engine.domain.components import (
    HistoricalEvidenceArtifact,
    KnowledgeRecord,
    ProvenanceReference,
    VisibilityScope,
)
from world_engine.domain.investigation_cases import (
    CaseEvidenceRequirement,
    CaseSourceReference,
    InvestigationCaseDefinition,
    InvestigationCaseRoute,
    WrongSuspectRelationEffect,
)
from world_engine.domain.investigation import EntityLocationFact, EntityStatusFact


@dataclass(frozen=True, slots=True)
class _OpeningText:
    case_title: str
    case_brief: str
    anchor_name: str
    anchor_description: str
    register_name: str
    register_description: str
    register_claim: str
    alibi_record_name: str
    alibi_record_description: str
    alibi_record_claim: str
    echo_claim: str
    echo_summary: str
    alibi_claim: str
    alibi_summary: str


_OPENING_TEXTS: dict[str, _OpeningText] = {
    "low_magic_feudal": _OpeningText(
        case_title="渡口停办传言案",
        case_brief=(
            "有人把“渡口早已停值”的旧说法当作现行消息复述。查清是谁传开这则失实消息，也查明另一名当值者是否在争议时刻亲手张贴停办告示；"
            "不在场只能排除那一次张贴，并不能说明此人后来是否听过或转述过传言。"
        ),
        anchor_name="渡口旧案封套",
        anchor_description="封面写着“停办传言”，里面只夹着待核对的线索目录。",
        register_name="渡口轮值簿副页",
        register_description="一张夹在轮值簿里的旧页，留有日期与交接印记。",
        register_claim="旧停办说法所标的时刻，轮值簿仍记有一笔已完成的渡口交接，旁边留着当值记号。",
        alibi_record_name="旧址点名簿移交副页",
        alibi_record_description="一份随旧档移交的点名簿副页，留有日期与轮换印记。",
        alibi_record_claim="渡口停办告示需要当面贴在公告点的时刻，另一名当值者登记在旧桥岗，副页记着同一时刻的留守签注。",
        echo_claim=(
            "有人曾在交班时复述“渡口早已停值”的说法。我也听见最初向你提起此事的那名当值者照着这句话说过。"
            "我不知道他是误信旧告示，还是只把听来的话再讲一遍。"
        ),
        echo_summary="关于停办传言的转述来路",
        alibi_claim=(
            "停办告示需要当面贴在渡口公告点的那个时刻，我在旧桥岗值守，没有亲手张贴它。"
            "点名簿记下了我的当值位置；这不能说明我后来是否听过或转述过那则传言。"
        ),
        alibi_summary="另一名当值者关于旧址轮值的说法",
    ),
    "modern_city": _OpeningText(
        case_title="车站停办传言案",
        case_brief=(
            "一则称车站已经停办的旧消息，被当作现场告示复述。查清是谁传开这则失实消息，并核实另一名值守者是否在争议时刻亲手张贴停办告示；"
            "不在场只涉及那次张贴，不能说明此人后来是否听过或转述过消息。"
        ),
        anchor_name="车站旧案封套",
        anchor_description="封面写着“停办传言”，内页列着几项尚待查证的记录。",
        register_name="站务交班簿摘页",
        register_description="一页留有班次编号和签收栏的旧站务记录。",
        register_claim="旧停办消息标注的生效时刻，交班簿仍记着一笔站台服务交接，签收栏有当班记号。",
        alibi_record_name="旧站务档案移交副页",
        alibi_record_description="一份随旧档移交的站务副页，留有班次编号和交接签名。",
        alibi_record_claim="停办告示需要由人在车站服务台张贴的时刻，另一名站务人员登记在旧维修点值守，副页有同一时刻的交接记号。",
        echo_claim=(
            "有人把“本站早已停办”的旧消息当成现场通知复述。我也听见最初向你提及此事的那位值守者照着这句话说过。"
            "也许他只是转述了一条过期调度消息；我不能判断他的用意。"
        ),
        echo_summary="关于停办消息的转述来路",
        alibi_claim=(
            "停办告示需要亲手贴在车站服务台的那个时刻，我在旧维修点值守，没有亲手张贴它。"
            "签领页记下了我的出勤位置；这不能说明我后来是否听过或转述过那则消息。"
        ),
        alibi_summary="另一名值守者关于旧址出勤的说法",
    ),
    "long_life_cultivation": _OpeningText(
        case_title="旧泉停分传言案",
        case_brief=(
            "一则称灵泉已经停分的旧说法，被当作现场通告复述。查清是谁传开这则失实消息，也核实另一名守门者是否在争议时刻亲手张贴停分告示；"
            "不在场只能排除那次张贴，不能说明此人后来是否听过或转述过传言。"
        ),
        anchor_name="灵泉旧案封套",
        anchor_description="封面题着“停分传言”，里面列着几项待查的旧记录。",
        register_name="灵泉分水轮次记页",
        register_description="一张留下时辰与闸口交接印记的旧分水记录。",
        register_claim="旧停分说法所标的换文时刻，记录上仍有一轮已完成的分水，旁边留着闸口当值印记。",
        alibi_record_name="旧址轮值名册移交副页",
        alibi_record_description="一份随旧档移交的轮值副页，时辰和交班符印仍可辨认。",
        alibi_record_claim="停分告示需要亲手贴在泉边公告处的时刻，另一名守门者登记在旧阵门值守，副页留有同一时刻的交班符印。",
        echo_claim=(
            "有人把“灵泉早已停分”的旧说法当作现场通告转述。我也听见最初向你提及此事的那名值守者复述过。"
            "也许他只是信了旧契传闻；我不知其用意。"
        ),
        echo_summary="关于停分传言的转述来路",
        alibi_claim=(
            "停分告示需要亲手贴在泉边公告处的那个时刻，我在旧阵门值守，没有亲手张贴它。"
            "轮值名册记下了我的位置；这不能说明我后来是否听过或转述过那则说法。"
        ),
        alibi_summary="另一名守门者关于旧阵门轮值的说法",
    ),
}


@dataclass(frozen=True, slots=True)
class OpeningCaseDraft:
    """Validated seed data ready for a catalog builder to add to a world."""

    anchor_id: UUID
    anchor_name: str
    anchor_components: dict[str, object]
    # Each entry is (entity_id, display_name, location_id, components).
    historical_items: tuple[tuple[UUID, str, UUID, dict[str, object]], ...]
    knowledge_records: tuple[dict[str, object], ...]


def build_opening_case(
    pack_id: str,
    pack_version: str,
    opening_id: UUID,
    discovery_site_id: UUID,
    witness_id: UUID,
    alternate_witness_id: UUID,
    entity_id: Callable[..., UUID],
) -> OpeningCaseDraft:
    """Build the three-route opening rumor case for a shipped background.

    The caller supplies the same deterministic UUID function used for other
    genesis entities. The alternate witness must already be a named character
    at the opening place. Nothing in the anchor's public text discloses the
    historical alibi's location; that trail remains behind the discovered
    historical site.
    """
    try:
        copy = _OPENING_TEXTS[pack_id]
    except KeyError as exc:
        raise ValueError(f"No authored opening investigation exists for pack {pack_id!r}.") from exc
    if not pack_version.strip():
        raise ValueError("An opening case needs a concrete pack version for provenance.")
    if opening_id == discovery_site_id:
        raise ValueError("The opening settlement and hidden discovery site must be distinct.")
    if witness_id == alternate_witness_id:
        raise ValueError("The opening case requires two distinct named witnesses.")

    pack_root = f"{pack_id}:{pack_version}:opening."
    effective_tick = -3_600

    def pack_source(suffix: str) -> CaseSourceReference:
        return CaseSourceReference(kind="pack", reference_id=f"{pack_root}{suffix}")

    def status_conflict(observation_root: str, claim_root: str) -> CaseEvidenceRequirement:
        return CaseEvidenceRequirement(
            subject_id=opening_id,
            predicate="entity_status",
            effective_tick=effective_tick,
            finding="conflict",
            observation_provenance=[pack_source(observation_root)],
            claim_provenance=[pack_source(claim_root)],
        )

    routes = [
        InvestigationCaseRoute(
            route_id="witness_repeat_archive_route",
            suspect_id=witness_id,
            finding="supports",
            requirements=[status_conflict("archive", "witness")],
        ),
        InvestigationCaseRoute(
            route_id="witness_repeat_register_route",
            suspect_id=witness_id,
            finding="supports",
            requirements=[status_conflict("register", "echo")],
        ),
        InvestigationCaseRoute(
            route_id="alternate_witness_not_physical_poster_route",
            suspect_id=alternate_witness_id,
            finding="rules_out",
            requirements=[CaseEvidenceRequirement(
                subject_id=alternate_witness_id,
                predicate="entity_location",
                effective_tick=effective_tick,
                finding="corroborated",
                observation_provenance=[pack_source("alibi.record")],
                claim_provenance=[pack_source("alibi.claim")],
            )],
        ),
    ]
    definition = InvestigationCaseDefinition(
        case_id=entity_id("case", "opening-investigation-rumor"),
        title=copy.case_title,
        brief=copy.case_brief,
        routes=routes,
        wrong_suspect_effect=WrongSuspectRelationEffect(dimension="trust", delta=-15),
    )

    anchor_id = entity_id("item", "opening-investigation-case")
    anchor_components: dict[str, object] = {
        "public_description": copy.anchor_description,
        "investigation_case": definition.model_dump(mode="json"),
    }

    register_item_id = entity_id("item", "opening-investigation-register")
    register_evidence_id = entity_id("evidence", "opening-investigation-register")
    register_evidence = HistoricalEvidenceArtifact(
        evidence_id=register_evidence_id,
        subject_id=opening_id,
        claim=copy.register_claim,
        fact=EntityStatusFact(value="active", effective_tick=effective_tick),
        provenance=[ProvenanceReference(kind="pack", reference_id=f"{pack_root}register")],
    )

    alibi_item_id = entity_id("item", "opening-investigation-alibi-record")
    alibi_evidence_id = entity_id("evidence", "opening-investigation-alibi-record")
    alibi_evidence = HistoricalEvidenceArtifact(
        evidence_id=alibi_evidence_id,
        subject_id=alternate_witness_id,
        claim=copy.alibi_record_claim,
        fact=EntityLocationFact(value=discovery_site_id, effective_tick=effective_tick),
        provenance=[ProvenanceReference(kind="pack", reference_id=f"{pack_root}alibi.record")],
    )

    historical_items = (
        (
            register_item_id,
            copy.register_name,
            opening_id,
            {
                "public_description": copy.register_description,
                "historical_evidence": register_evidence.model_dump(mode="json"),
            },
        ),
        (
            alibi_item_id,
            copy.alibi_record_name,
            opening_id,
            {
                "public_description": copy.alibi_record_description,
                "historical_evidence": alibi_evidence.model_dump(mode="json"),
            },
        ),
    )

    echo_record = KnowledgeRecord(
        id=entity_id("knowledge", "opening-investigation-echo"),
        holder_id=alternate_witness_id,
        subject_id=opening_id,
        claim=copy.echo_claim,
        summary=copy.echo_summary,
        confidence="0.65",
        kind="rumor",
        fact=EntityStatusFact(value="retired", effective_tick=effective_tick),
        disclosure_policy="if_asked",
        visibility=VisibilityScope(policy="private"),
        provenance=[ProvenanceReference(kind="pack", reference_id=f"{pack_root}echo")],
    )
    alibi_record = KnowledgeRecord(
        id=entity_id("knowledge", "opening-investigation-alibi-claim"),
        holder_id=alternate_witness_id,
        subject_id=alternate_witness_id,
        claim=copy.alibi_claim,
        summary=copy.alibi_summary,
        confidence="0.65",
        kind="claim",
        fact=EntityLocationFact(value=discovery_site_id, effective_tick=effective_tick),
        disclosure_policy="if_asked",
        visibility=VisibilityScope(policy="private"),
        provenance=[ProvenanceReference(kind="pack", reference_id=f"{pack_root}alibi.claim")],
    )

    return OpeningCaseDraft(
        anchor_id=anchor_id,
        anchor_name=copy.anchor_name,
        anchor_components=anchor_components,
        historical_items=historical_items,
        knowledge_records=(
            echo_record.model_dump(mode="json", exclude_none=True),
            alibi_record.model_dump(mode="json", exclude_none=True),
        ),
    )
