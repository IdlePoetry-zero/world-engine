from __future__ import annotations

from uuid import UUID

from world_engine.domain.autonomous_vacancy import AUTONOMOUS_VACANCY_BALLOT_AUTHORITY
from world_engine.domain.components import GovernanceComponent, KnowledgeRecord, OfficeRecord
from world_engine.domain.models import WorldEntity, WorldState
from world_engine.simulation.contracts import LongActionPlan, SimulationContext
from world_engine.simulation.engine import SimulationEngine


PLAYER_ID = UUID("10000000-0000-4000-8000-000000000001")
VISIBLE_MEMBER_ID = UUID("20000000-0000-4000-8000-000000000002")
HIDDEN_UNKNOWN_ID = UUID("30000000-0000-4000-8000-000000000003")
HIDDEN_KNOWN_ID = UUID("40000000-0000-4000-8000-000000000004")
DEAD_LEADER_ID = UUID("50000000-0000-4000-8000-000000000005")
RETIRED_MEMBER_ID = UUID("60000000-0000-4000-8000-000000000006")
ORGANIZATION_ID = UUID("70000000-0000-4000-8000-000000000007")


def _state(*, player_id=PLAYER_ID, succession_policy="election", charter_ballot=False):
    governance = GovernanceComponent(
        members=[
            PLAYER_ID,
            VISIBLE_MEMBER_ID,
            HIDDEN_UNKNOWN_ID,
            HIDDEN_KNOWN_ID,
            DEAD_LEADER_ID,
            RETIRED_MEMBER_ID,
        ],
        leader_id=DEAD_LEADER_ID,
        offices=([OfficeRecord(
            office_id="office.appointer",
            title="Appointer",
            holder_id=PLAYER_ID,
            authority=["authority.appoint"],
        )] if succession_policy == "none" else []),
        succession_policy=succession_policy,
        authority_tags=[AUTONOMOUS_VACANCY_BALLOT_AUTHORITY] if charter_ballot else [],
    )
    entities = {
        PLAYER_ID: WorldEntity(id=PLAYER_ID, kind="character", name="Player"),
        VISIBLE_MEMBER_ID: WorldEntity(id=VISIBLE_MEMBER_ID, kind="character", name="Visible Member"),
        HIDDEN_UNKNOWN_ID: WorldEntity(
            id=HIDDEN_UNKNOWN_ID, kind="character", name="Unknown Hidden Member",
            components={"hidden": True},
        ),
        HIDDEN_KNOWN_ID: WorldEntity(
            id=HIDDEN_KNOWN_ID, kind="character", name="Known Hidden Member",
            components={"hidden": True},
        ),
        DEAD_LEADER_ID: WorldEntity(
            id=DEAD_LEADER_ID, kind="character", name="Former Leader", status="dead",
        ),
        RETIRED_MEMBER_ID: WorldEntity(
            id=RETIRED_MEMBER_ID, kind="character", name="Retired Member", status="retired",
        ),
        ORGANIZATION_ID: WorldEntity(
            id=ORGANIZATION_ID, kind="organization", name="Council",
            components={"governance": governance.model_dump(mode="json")},
        ),
    }
    return WorldState(
        player_id=player_id,
        entities=entities,
        knowledge=[KnowledgeRecord(
            id=UUID("80000000-0000-4000-8000-000000000008"),
            holder_id=PLAYER_ID,
            subject_id=HIDDEN_KNOWN_ID,
            claim="The player has observed this member.",
            received_tick=0,
            kind="observation",
        )],
    )


def _request(state):
    resolution = SimulationEngine().plan_window(
        state,
        LongActionPlan(actor_id=PLAYER_ID, target_tick=3600, activity="wait"),
        SimulationContext(
            world_id=UUID("90000000-0000-4000-8000-000000000009"),
            branch_id=UUID("a0000000-0000-4000-8000-00000000000a"),
            command_id=UUID("b0000000-0000-4000-8000-00000000000b"),
            world_seed="succession-visibility-test",
            state_version=0,
        ),
    )
    assert resolution.outcome == "decision_required"
    assert resolution.events == ()
    return resolution.decision_requests[0]


def test_model_succession_considers_hidden_registered_members_but_player_options_do_not_leak_them():
    model_request = _request(_state(player_id=None))
    assert model_request.decision_owner == "model"
    assert model_request.evidence_ids == (ORGANIZATION_ID, DEAD_LEADER_ID)
    model_target = model_request.resolution_target
    assert model_target is not None
    assert set(model_target.candidate_ids) == {
        PLAYER_ID, VISIBLE_MEMBER_ID, HIDDEN_UNKNOWN_ID, HIDDEN_KNOWN_ID,
    }
    assert any(option.label == "Appoint Unknown Hidden Member" for option in model_request.options)

    player_request = _request(_state(player_id=PLAYER_ID))
    assert player_request.decision_owner == "user"
    assert player_request.evidence_ids == ()
    player_target = player_request.resolution_target
    assert player_target is not None
    assert set(player_target.candidate_ids) == {
        PLAYER_ID, VISIBLE_MEMBER_ID, HIDDEN_KNOWN_ID,
    }
    labels = {option.label for option in player_request.options}
    assert "Appoint Known Hidden Member" in labels
    assert "Appoint Unknown Hidden Member" not in labels
    assert any(option.option_id == "leave_vacant" for option in player_request.options)


def test_none_policy_keeps_legacy_leave_only_candidate_snapshot():
    for player_id, owner in ((PLAYER_ID, "user"), (None, "model")):
        request = _request(_state(player_id=player_id, succession_policy="none"))
        assert request.decision_owner == owner
        assert [option.option_id for option in request.options] == ["leave_vacant"]
        target = request.resolution_target
        assert target is not None
        expected_candidates = {PLAYER_ID, VISIBLE_MEMBER_ID}
        if player_id == PLAYER_ID:
            expected_candidates.add(HIDDEN_KNOWN_ID)
        assert set(target.candidate_ids) == expected_candidates


def test_opted_in_death_succession_can_only_clear_the_seat_before_a_formal_ballot():
    request = _request(_state(charter_ballot=True))
    assert request.decision_owner == "model"
    assert [choice.option_id for choice in request.options] == ["leave_vacant"]
    assert request.resolution_target is not None and not request.resolution_target.candidate_ids
