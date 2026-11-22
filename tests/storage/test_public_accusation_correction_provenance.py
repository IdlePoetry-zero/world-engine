from __future__ import annotations

from decimal import Decimal
from uuid import uuid4, uuid5

import pytest

from world_engine.domain.components import KnowledgeRecord
from world_engine.domain.errors import AccessDenied, InvalidEvent
from world_engine.domain.gameplay_events import (
    AccusationRepairTrustProof,
    InvestigationCaseClosedEvent,
    PublicAccusationCorrectionRecordedEvent,
)
from world_engine.domain.investigation import EvidenceAssessment, WEIGH_EVIDENCE_POLICY, assessment_text
from world_engine.domain.investigation_cases import (
    CaseEvidenceRequirement,
    InvestigationCaseDefinition,
    InvestigationCaseFile,
    PublicAccusationCorrection,
    case_definition_digest,
)
from world_engine.domain.models import AccessScope, CommandEnvelope, MembershipRole, NewWorld, WorldEntity, WorldState
from world_engine.rules import RuleContext, RuleEngine
from world_engine.storage.database import Database
from world_engine.storage.repository import WorldRepository


def _make_world(repository, scope, *, wrong_suspect_delta=-12):
    place_id, actor_id, supported_id, accused_id, subject_id, anchor_id = (uuid4() for _ in range(6))
    primary_observation_ref = "pack.case.archive.primary"
    primary_claim_ref = "pack.case.witness.primary"
    alternate_observation_ref = "pack.case.archive.alternate"
    alternate_claim_ref = "pack.case.witness.alternate"
    fact = {"predicate": "entity_status", "value": "active", "effective_tick": -3_600}

    observation = KnowledgeRecord(
        id=uuid4(), owner_id=actor_id, subject_id=subject_id,
        claim="The bell was present in the hall.", kind="observation", confidence=1,
        source_refs=[uuid4()], source_event_id=uuid4(),
        provenance=[{"kind": "pack", "reference_id": primary_observation_ref}], received_tick=0,
        fact=fact, visibility={"policy": "private"},
    )
    claim = KnowledgeRecord(
        id=uuid4(), owner_id=actor_id, subject_id=subject_id,
        claim="A witness remembers the bell at the hall.", kind="claim", confidence="0.7",
        source_event_id=uuid4(), provenance=[{"kind": "pack", "reference_id": primary_claim_ref}],
        received_tick=0, fact=fact, visibility={"policy": "private"},
    )
    assessed_claim, summary = assessment_text(observation.fact, "corroborated")
    assessment = KnowledgeRecord(
        id=uuid4(), owner_id=actor_id, subject_id=subject_id,
        claim=assessed_claim, summary=summary, confidence=1,
        source_refs=[observation.id, claim.id], received_tick=0, kind="belief",
        assessment=EvidenceAssessment(
            finding="corroborated", observation_id=observation.id, claim_id=claim.id,
            predicate="entity_status", effective_tick=-3_600,
        ),
        provenance=[{"kind": "rule", "reference_id": WEIGH_EVIDENCE_POLICY}],
        visibility={"policy": "private"},
    )
    primary_requirement = CaseEvidenceRequirement(
        subject_id=subject_id, predicate="entity_status", effective_tick=-3_600,
        finding="corroborated",
        observation_provenance=[{"kind": "pack", "reference_id": primary_observation_ref}],
        claim_provenance=[{"kind": "pack", "reference_id": primary_claim_ref}],
    )
    alternate_requirement = CaseEvidenceRequirement(
        subject_id=subject_id, predicate="entity_status", effective_tick=-3_600,
        finding="corroborated",
        observation_provenance=[{"kind": "pack", "reference_id": alternate_observation_ref}],
        claim_provenance=[{"kind": "pack", "reference_id": alternate_claim_ref}],
    )
    wrong_suspect_effect = (
        None if wrong_suspect_delta is None
        else {"dimension": "trust", "delta": wrong_suspect_delta}
    )
    definition = InvestigationCaseDefinition(
        case_id=uuid4(), title="钟楼旧案", brief="查明冬宴前后铃铛的去向。",
        routes=[
            {"route_id": "supported-route", "suspect_id": str(supported_id), "finding": "supports",
             "requirements": [alternate_requirement.model_dump(mode="json")]},
            {"route_id": "cleared-route", "suspect_id": str(accused_id), "finding": "rules_out",
             "requirements": [primary_requirement.model_dump(mode="json")]},
        ],
        wrong_suspect_effect=wrong_suspect_effect,
    )
    entities = {
        place_id: WorldEntity(id=place_id, kind="place", name="旧广场", status="active"),
        actor_id: WorldEntity(id=actor_id, kind="character", name="玩家", status="active", location_id=place_id),
        supported_id: WorldEntity(id=supported_id, kind="character", name="档案员", status="active", location_id=place_id),
        accused_id: WorldEntity(id=accused_id, kind="character", name="钟楼守卫", status="active", location_id=place_id),
        subject_id: WorldEntity(id=subject_id, kind="item", name="旧铃", status="active", location_id=place_id),
        anchor_id: WorldEntity(
            id=anchor_id, kind="item", name="调查告示", status="active", location_id=place_id,
            components={"investigation_case": definition.model_dump(mode="json")},
        ),
    }
    state = WorldState(
        player_id=actor_id, entities=entities,
        knowledge=[observation, claim, assessment],
    )
    created = repository.create_world(scope, NewWorld(
        title="Public accusation provenance", ruleset_version="public-accusation-provenance.v1", state=state,
    ))
    return created, {
        "actor": actor_id,
        "accused": accused_id,
        "anchor": anchor_id,
        "assessment": assessment.id,
        "case_id": definition.case_id,
        "place": place_id,
    }


def _commit_action(repository, scope, created, actor_id, payload, *, branch=None, causes=()):
    branch = branch or created.branch
    head = repository.read_world(scope, created.world.world_id, branch.branch_id)
    command_id = uuid4()
    context = RuleContext(
        world_id=created.world.world_id,
        branch_id=branch.branch_id,
        command_id=command_id,
        actor_id=actor_id,
        ruleset_version=created.world.ruleset_version,
    )
    resolution = RuleEngine().evaluate(head.state, payload, context)
    assert resolution.outcome.status == "succeeded", resolution.outcome
    command = CommandEnvelope(
        command_id=command_id,
        actor_id=actor_id,
        idempotency_key=f"storage-case-action:{command_id}",
        expected_state_version=head.branch.state_version,
        command_type=f"action.{payload['kind']}",
        payload=payload,
        causation_ids=causes,
    )
    return repository.commit_command(
        scope, created.world.world_id, branch.branch_id, command, resolution.events,
    )


def _close_public_case(repository, scope, created, people):
    _commit_action(repository, scope, created, people["actor"], {
        "kind": "open_investigation_case", "anchor_id": str(people["anchor"]),
    })
    opened = repository.read_world(scope, created.world.world_id, created.branch.branch_id)
    case_file = opened.state.investigation_cases[0]
    _commit_action(repository, scope, created, people["actor"], {
        "kind": "attach_case_evidence", "case_file_id": str(case_file.id),
        "assessment_id": str(people["assessment"]),
    })
    attached = repository.read_world(scope, created.world.world_id, created.branch.branch_id)
    assert attached.state.investigation_cases[0].revision == 2
    before_close_child = repository.fork(
        scope, created.world.world_id, created.branch.branch_id, attached.last_seq, "before public close",
    )
    result = _commit_action(repository, scope, created, people["actor"], {
        "kind": "close_investigation_case", "case_file_id": str(case_file.id),
        "suspect_id": str(people["accused"]), "public_accusation": True,
    })
    closure_record = next(row for row in result.events if row.type == "core.investigation_case_closed.v1")
    closure_event = InvestigationCaseClosedEvent.model_validate({
        "type": closure_record.type,
        "event_id": closure_record.event_id,
        "visibility": closure_record.visibility,
        **closure_record.payload,
    })
    after_close = repository.read_world(scope, created.world.world_id, created.branch.branch_id)
    inherited = repository.fork(
        scope, created.world.world_id, created.branch.branch_id, after_close.last_seq, "after public close",
    )
    return case_file.id, closure_event, before_close_child, inherited


def _correction_command(created, actor_id, case_file_id, closure_event, location_id, branch, source_event_id, *, causes):
    command_id = uuid4()
    event_id = uuid5(command_id, f"{created.world.ruleset_version}:investigation_public_accusation_corrected")
    record = PublicAccusationCorrection(
        event_id=event_id,
        case_file_id=case_file_id,
        expected_revision=closure_event.expected_revision + 1,
        source_closure_event_id=source_event_id,
        actor_id=actor_id,
        suspect_id=closure_event.suspect_id,
        corrected_tick=closure_event.closed_tick,
        location_id=location_id,
    )
    event = PublicAccusationCorrectionRecordedEvent(
        event_id=event_id,
        world_id=created.world.world_id,
        branch_id=branch.branch_id,
        command_id=command_id,
        ruleset_version=created.world.ruleset_version,
        actor_id=actor_id,
        record=record,
    )
    payload = {
        "kind": "correct_public_accusation",
        "case_file_id": str(case_file_id),
        "expected_revision": record.expected_revision,
        "source_closure_event_id": str(source_event_id),
    }
    head_version = branch.state_version
    return CommandEnvelope(
        command_id=command_id,
        actor_id=actor_id,
        idempotency_key=f"correct-public-accusation:{command_id}",
        expected_state_version=head_version,
        command_type="action.correct_public_accusation",
        payload=payload,
        causation_ids=causes,
    ), event


def test_correction_requires_exact_closure_in_current_branch_lineage_and_replays(tmp_path):
    database = Database(f"sqlite:///{(tmp_path / 'world.sqlite').as_posix()}")
    database.create_schema()
    repository = WorldRepository(database)
    scope = AccessScope(tenant_id=uuid4(), user_id=uuid4())
    try:
        created, people = _make_world(repository, scope)
        case_file_id, closure_event, before_close, after_close = _close_public_case(
            repository, scope, created, people,
        )
        assert repository.find_public_accusation_closures(
            scope, created.world.world_id, before_close.branch_id, people["actor"], (case_file_id,),
        ) == {}
        with repository._transaction() as session:
            before_close_row = repository._get_branch(
                session, created.world.world_id, before_close.branch_id,
            )
            assert repository._read_public_accusation_closure_in_lineage(
                session, created.world.world_id, before_close_row, closure_event.event_id,
                through_seq=before_close_row.head_seq,
            ) is None
        assert repository.find_public_accusation_closures(
            scope, created.world.world_id, after_close.branch_id, people["actor"], (case_file_id,),
        ) == {case_file_id: closure_event.event_id}

        child_head = repository.read_world(scope, created.world.world_id, after_close.branch_id)
        mismatched_command, mismatched_event = _correction_command(
            created, people["actor"], case_file_id, closure_event, people["place"], after_close,
            uuid4(), causes=[closure_event.event_id],
        )
        with pytest.raises(InvalidEvent, match="causally reference exactly its source closure"):
            repository.commit_command(
                scope, created.world.world_id, after_close.branch_id,
                mismatched_command, [mismatched_event],
            )
        assert repository.read_world(scope, created.world.world_id, after_close.branch_id).state_hash == child_head.state_hash

        command, event = _correction_command(
            created, people["actor"], case_file_id, closure_event, people["place"], after_close,
            closure_event.event_id, causes=[closure_event.event_id],
        )
        committed = repository.commit_command(
            scope, created.world.world_id, after_close.branch_id, command, [event],
        )
        corrected_head = repository.read_world(scope, created.world.world_id, after_close.branch_id)
        assert corrected_head.state.public_accusation_corrections[0].source_closure_event_id == closure_event.event_id
        assert repository.find_public_accusation_closures(
            scope, created.world.world_id, after_close.branch_id, people["actor"], (case_file_id,),
        ) == {}
        replayed = repository.replay(
            scope, created.world.world_id, after_close.branch_id, from_genesis=True,
        )
        assert replayed.state_hash == committed.state_hash
        assert replayed.state.public_accusation_corrections == corrected_head.state.public_accusation_corrections
    finally:
        database.dispose()


def test_new_world_cannot_seed_correction_without_source_history(tmp_path):
    database = Database(f"sqlite:///{(tmp_path / 'seeded.sqlite').as_posix()}")
    database.create_schema()
    repository = WorldRepository(database)
    scope = AccessScope(tenant_id=uuid4(), user_id=uuid4())
    try:
        created, people = _make_world(repository, scope)
        state = repository.read_world(scope, created.world.world_id, created.branch.branch_id).state
        anchor = state.entities[people["anchor"]]
        case_definition = InvestigationCaseDefinition.model_validate(anchor.components["investigation_case"])
        case_file_id, event_id, source_event_id = (uuid4() for _ in range(3))
        case_file = InvestigationCaseFile(
            id=case_file_id,
            case_id=people["case_id"],
            anchor_id=people["anchor"],
            owner_id=people["actor"],
            definition_hash=case_definition_digest(case_definition),
            title=case_definition.title,
            brief=case_definition.brief,
            status="closed",
            revision=3,
            opened_tick=0,
            updated_tick=0,
            closed_tick=0,
            outcome="disconfirmed",
            suspect_id=people["accused"],
            public_accusation=True,
        )
        correction = PublicAccusationCorrection(
            event_id=event_id,
            case_file_id=case_file_id,
            expected_revision=3,
            source_closure_event_id=source_event_id,
            actor_id=people["actor"],
            suspect_id=people["accused"],
            corrected_tick=0,
            location_id=people["place"],
        )
        seeded_state = WorldState.model_validate({
            **state.model_dump(mode="json"),
            "investigation_cases": [case_file.model_dump(mode="json")],
            "public_accusation_corrections": [correction.model_dump(mode="json")],
        })
        with pytest.raises(InvalidEvent, match="cannot seed public-accusation correction provenance"):
            repository.create_world(scope, NewWorld(
                title="Seeded correction",
                state=seeded_state,
            ))
    finally:
        database.dispose()


def test_repair_proof_is_authorized_lineage_bound_and_replayed_with_exact_recovery(tmp_path):
    database = Database(f"sqlite:///{(tmp_path / 'repair.sqlite').as_posix()}")
    database.create_schema()
    repository = WorldRepository(database)
    scope = AccessScope(tenant_id=uuid4(), user_id=uuid4())
    try:
        created, people = _make_world(repository, scope)
        case_file_id, closure_event, _, inherited = _close_public_case(
            repository, scope, created, people,
        )
        command, correction_event = _correction_command(
            created, people["actor"], case_file_id, closure_event, people["place"], inherited,
            closure_event.event_id, causes=[closure_event.event_id],
        )
        repository.commit_command(
            scope, created.world.world_id, inherited.branch_id, command, [correction_event],
        )
        correction_id = correction_event.event_id
        offer_commit = _commit_action(
            repository, scope, created, people["actor"],
            {"kind": "offer_accusation_repair", "correction_event_id": str(correction_id),
             "statement": "I was wrong, and I will take responsibility."},
            branch=inherited,
            causes=[correction_id],
        )
        offer = repository.read_world(
            scope, created.world.world_id, inherited.branch_id,
        ).state.accusation_repair_offers[0]

        # The offer lives on a child branch; its source closure and correction are inherited.
        hosted_proof = repository.find_accusation_repair_trust_proof(
            scope, created.world.world_id, inherited.branch_id, offer.offer_id,
            people["accused"], requested_by=people["actor"],
        )
        assert hosted_proof is not None
        assert hosted_proof.case_file_id == case_file_id
        assert hosted_proof.source_closure_event_id == closure_event.event_id
        assert hosted_proof.accuser_id == people["actor"]
        assert hosted_proof.suspect_id == people["accused"]
        assert hosted_proof.baseline_trust == Decimal("0")
        assert hosted_proof.post_effect_trust == Decimal("-12")
        assert hosted_proof.source_relation_effect_event_id != closure_event.event_id
        assert repository.find_accusation_repair_trust_proof(
            scope, created.world.world_id, inherited.branch_id, offer.offer_id, people["accused"],
        ) == hosted_proof
        assert repository.find_accusation_repair_trust_proof(
            scope, created.world.world_id, inherited.branch_id, offer.offer_id, people["actor"],
        ) is None

        # A player controlling the target may resolve directly; an unrelated player may not.
        direct_user_id = uuid4()
        repository.grant_membership(
            scope, created.world.world_id, direct_user_id, MembershipRole.PLAYER,
            controlled_actor_ids=[people["accused"]],
        )
        direct_scope = AccessScope(tenant_id=scope.tenant_id, user_id=direct_user_id)
        assert repository.find_accusation_repair_trust_proof(
            direct_scope, created.world.world_id, inherited.branch_id, offer.offer_id, people["accused"],
        ) == hosted_proof
        unrelated_user_id = uuid4()
        repository.grant_membership(
            scope, created.world.world_id, unrelated_user_id, MembershipRole.PLAYER,
        )
        with pytest.raises(AccessDenied, match="does not control"):
            repository.find_accusation_repair_trust_proof(
                AccessScope(tenant_id=scope.tenant_id, user_id=unrelated_user_id), created.world.world_id,
                inherited.branch_id, offer.offer_id, people["accused"],
            )
        with pytest.raises(AccessDenied, match="user-controlled character"):
            repository.find_accusation_repair_trust_proof(
                scope, created.world.world_id, inherited.branch_id, offer.offer_id,
                people["accused"], requested_by=people["actor"],
            )
        repository.grant_membership(
            scope, created.world.world_id, direct_user_id, MembershipRole.PLAYER,
        )

        def plan_hosted_accept(proof):
            head = repository.read_world(scope, created.world.world_id, inherited.branch_id)
            command_id = uuid4()
            context = RuleContext(
                world_id=created.world.world_id,
                branch_id=inherited.branch_id,
                command_id=command_id,
                actor_id=people["accused"],
                ruleset_version=created.world.ruleset_version,
                accusation_repair_requested_by=people["actor"],
                accusation_repair_trust_proof=proof,
            )
            resolution = RuleEngine().evaluate(head.state, {
                "kind": "resolve_accusation_repair",
                "offer_id": str(offer.offer_id),
                "expected_revision": offer.revision,
                "operation": "accept",
            }, context)
            assert resolution.outcome.status == "succeeded", resolution.outcome
            response = next(event for event in resolution.events if event.type == "core.accusation_repair_resolved.v1")
            causes = [offer.offer_id]
            if response.trust_recovery is not None and response.trust_recovery.applied_delta > 0:
                causes.append(response.trust_recovery.proof.source_relation_effect_event_id)
            command = CommandEnvelope(
                command_id=command_id,
                actor_id=people["actor"],
                idempotency_key=f"hosted-repair:{command_id}",
                expected_state_version=head.branch.state_version,
                command_type="action.request_accusation_repair_response",
                payload={"kind": "request_accusation_repair_response", "offer_id": str(offer.offer_id),
                         "expected_revision": offer.revision},
                causation_ids=causes,
            )
            return resolution.events, command

        forged_proof = AccusationRepairTrustProof.model_validate({
            **hosted_proof.model_dump(mode="python"),
            "baseline_trust": Decimal("1"),
        })
        accepted_events, accepted_command = plan_hosted_accept(hosted_proof)
        unproven_events = [accepted_events[0].model_copy(update={"trust_recovery": None}), accepted_events[1]]
        unproven_command = accepted_command.model_copy(update={"causation_ids": [offer.offer_id]})
        unchanged_hash = repository.read_world(
            scope, created.world.world_id, inherited.branch_id,
        ).state_hash
        with pytest.raises(InvalidEvent, match="Accusation repair is unavailable"):
            repository.commit_command(
                scope, created.world.world_id, inherited.branch_id, unproven_command, unproven_events,
            )
        assert repository.read_world(
            scope, created.world.world_id, inherited.branch_id,
        ).state_hash == unchanged_hash

        forged_events, forged_command = plan_hosted_accept(forged_proof)
        with pytest.raises(InvalidEvent, match="does not match its actual closure harm"):
            repository.commit_command(
                scope, created.world.world_id, inherited.branch_id, forged_command, forged_events,
            )
        assert repository.read_world(
            scope, created.world.world_id, inherited.branch_id,
        ).state_hash == unchanged_hash

        committed = repository.commit_command(
            scope, created.world.world_id, inherited.branch_id, accepted_command, accepted_events,
        )
        accepted_head = repository.read_world(scope, created.world.world_id, inherited.branch_id)
        relation = next(item for item in accepted_head.state.relations
                        if item.from_id == people["accused"] and item.to_id == people["actor"])
        assert relation.dimensions["trust"] == Decimal("-8")
        assert accepted_head.state.accusation_repair_offers[0].status == "accepted"
        assert repository.find_accusation_repair_trust_proof(
            scope, created.world.world_id, inherited.branch_id, offer.offer_id,
            people["accused"], requested_by=people["actor"],
        ) is None
        replayed = repository.replay(
            scope, created.world.world_id, inherited.branch_id, from_genesis=True,
        )
        assert replayed.state_hash == committed.state_hash
        replay_relation = next(item for item in replayed.state.relations
                               if item.from_id == people["accused"] and item.to_id == people["actor"])
        assert replay_relation.dimensions["trust"] == Decimal("-8")
    finally:
        database.dispose()


def test_positive_closure_effect_does_not_produce_a_negative_trust_proof(tmp_path):
    database = Database(f"sqlite:///{(tmp_path / 'nonnegative-repair.sqlite').as_posix()}")
    database.create_schema()
    repository = WorldRepository(database)
    scope = AccessScope(tenant_id=uuid4(), user_id=uuid4())
    try:
        created, people = _make_world(repository, scope, wrong_suspect_delta=5)
        case_file_id, closure_event, _, inherited = _close_public_case(
            repository, scope, created, people,
        )
        command, correction_event = _correction_command(
            created, people["actor"], case_file_id, closure_event, people["place"], inherited,
            closure_event.event_id, causes=[closure_event.event_id],
        )
        repository.commit_command(
            scope, created.world.world_id, inherited.branch_id, command, [correction_event],
        )
        correction_id = correction_event.event_id
        _commit_action(
            repository, scope, created, people["actor"],
            {"kind": "offer_accusation_repair", "correction_event_id": str(correction_id),
             "statement": "I was wrong, and I will take responsibility."},
            branch=inherited,
            causes=[correction_id],
        )
        offer = repository.read_world(
            scope, created.world.world_id, inherited.branch_id,
        ).state.accusation_repair_offers[0]
        assert repository.find_accusation_repair_trust_proof(
            scope, created.world.world_id, inherited.branch_id, offer.offer_id,
            people["accused"], requested_by=people["actor"],
        ) is None

        accepted = _commit_action(
            repository, scope, created, people["accused"],
            {"kind": "resolve_accusation_repair", "offer_id": str(offer.offer_id),
             "expected_revision": offer.revision, "operation": "accept"},
            branch=inherited,
            causes=[offer.offer_id],
        )
        assert [event.type for event in accepted.events] == ["core.accusation_repair_resolved.v1"]
        accepted_head = repository.read_world(scope, created.world.world_id, inherited.branch_id)
        relation = next(item for item in accepted_head.state.relations
                        if item.from_id == people["accused"] and item.to_id == people["actor"])
        assert relation.dimensions["trust"] == Decimal("5")
        assert accepted_head.state.accusation_repair_offers[0].status == "accepted"
        replayed = repository.replay(
            scope, created.world.world_id, inherited.branch_id, from_genesis=True,
        )
        assert replayed.state_hash == accepted_head.state_hash
    finally:
        database.dispose()
