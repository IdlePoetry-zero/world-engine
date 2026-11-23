from copy import deepcopy
from uuid import uuid4

import pytest

from world_engine.application.visibility import ProjectionError, project_event, project_player_state
from world_engine.application.actions import action_scene
from world_engine.domain.components import HistoricalEvidenceArtifact
from world_engine.domain.models import WorldEntity, WorldState


WORK_SHIFT_POSTING_ID = "41000000-0000-4000-8000-000000000014"
WORK_SHIFT_ASSIGNMENT_ID = "51000000-0000-4000-8000-000000000015"
WORK_SHIFT_EMPLOYER_ID = "61000000-0000-4000-8000-000000000016"
WORK_SHIFT_WORKER_ACCOUNT_ID = "71000000-0000-4000-8000-000000000017"
WORK_SHIFT_EMPLOYER_ACCOUNT_ID = "81000000-0000-4000-8000-000000000018"
WORK_SHIFT_RESOURCE_ID = "core.silver"


def state():
    return {
        "entities": {
            "p": {"id": "p", "kind": "character", "name": "玩家", "location_id": "town", "components": {"health": 90}},
            "n": {"id": "n", "kind": "character", "name": "商人", "location_id": "town", "components": {"public_role": "商人", "secret": "凶手", "health": 12}},
            "hidden": {"id": "hidden", "kind": "character", "name": "刺客", "location_id": "town", "components": {"hidden": True}},
            "far": {"id": "far", "kind": "character", "name": "远人", "location_id": "remote", "components": {}},
            "town": {"id": "town", "kind": "place", "name": "小镇", "components": {}},
        },
        "resources": {
            "mine": {"owner_id": "p", "resource_id": "coin", "balance": "5"},
            "other": {"owner_id": "n", "balance": "900"},
            "production": {"owner_id": "p", "account_type": "production_source",
                           "resource_id": "grain", "balance": "-12", "credit_limit": "100"},
        },
        "knowledge": [{"owner_id": "p", "claim": "听说有凶手", "secret_truth": "商人是凶手"}, {"owner_id": "n", "claim": "我是凶手"}],
        "goals": [{"owner_id": "n", "title": "灭口"}],
        "relations": [{"from_id": "n", "to_id": "p", "dimensions": {"hatred": 90}}],
        "scheduled_events": [{"title": "明日刺杀"}],
        "metadata": {"pack_id": "test", "killer": "n"},
    }


def test_scene_reunion_shows_public_hours_and_only_new_player_known_facts():
    world = state()
    world["current_tick"] = 100
    npc = world["entities"]["n"]
    npc["status"] = "active"
    npc["components"]["daily_routine"] = {"windows": [
        {"window_id": "public", "place_id": "town", "activity": "work",
         "start_second": 1000, "duration_seconds": 60, "public": True},
        {"window_id": "hidden", "place_id": "town", "activity": "rest",
         "start_second": 2000, "duration_seconds": 60},
        {"window_id": "elsewhere", "place_id": "remote", "activity": "study",
         "start_second": 3000, "duration_seconds": 60, "public": True},
    ]}
    npc["components"]["routine_mobility"] = {"cadences": {"public": {"every_days": 3, "phase_day": 2}}}
    world["knowledge"] = [
        {"id": "contact", "holder_id": "p", "subject_id": "n", "kind": "memory", "received_tick": 40,
         "claim": "上次交谈", "provenance": [{"kind": "observation", "reference_id": "previous"}]},
        {"id": "old", "holder_id": "p", "subject_id": "n", "kind": "observation", "received_tick": 20,
         "claim": "老事"},
        {"id": "recent", "owner_id": "p", "subject_id": "n", "kind": "observation", "received_tick": 80,
         "claim": "我看到商人收起了摊位。"},
        {"id": "secret", "owner_id": "n", "subject_id": "n", "kind": "observation", "received_tick": 95,
         "claim": "私事"},
    ]
    projected = project_player_state(world, "p")["scene_npc_readback"]
    assert projected == {"n": {
        "public_schedule": [{"place_name": "小镇", "activity": "work", "start_second": 1000,
                             "duration_seconds": 60, "every_days": 3}],
        "known_changes": [{"claim": "我看到商人收起了摊位。", "received_tick": "80"}],
        "last_contact_tick": "40",
    }}
    npc["location_id"] = "remote"
    assert "scene_npc_readback" not in project_player_state(world, "p")


def work_shift_state():
    world = state()
    world["current_tick"] = 600
    world["entities"]["p"]["status"] = "active"
    world["entities"]["town"]["status"] = "active"
    world["entities"][WORK_SHIFT_EMPLOYER_ID] = {
        "id": WORK_SHIFT_EMPLOYER_ID,
        "kind": "organization",
        "name": "城镇档案处",
        "location_id": "town",
        "status": "active",
        "components": {"treasury": {"accounts": {WORK_SHIFT_RESOURCE_ID: WORK_SHIFT_EMPLOYER_ACCOUNT_ID}}},
    }
    world["resources"].update({
        WORK_SHIFT_WORKER_ACCOUNT_ID: {
            "owner_id": "p", "resource_id": WORK_SHIFT_RESOURCE_ID, "balance": "4.25", "account_type": "holder",
        },
        WORK_SHIFT_EMPLOYER_ACCOUNT_ID: {
            "owner_id": WORK_SHIFT_EMPLOYER_ID, "resource_id": WORK_SHIFT_RESOURCE_ID,
            "balance": "100.00", "account_type": "holder",
        },
    })
    world["work_postings"] = [{
        "posting_id": WORK_SHIFT_POSTING_ID,
        "employer_id": WORK_SHIFT_EMPLOYER_ID,
        "location_id": "town",
        "title": "档案登记员",
        "required_skill": None,
        "slots": 1,
        "valid_from_tick": 0,
        "valid_until_tick": None,
        "status": "open",
        "revision": 3,
    }]
    world["work_assignments"] = [{
        "assignment_id": WORK_SHIFT_ASSIGNMENT_ID,
        "actor_id": "p",
        "posting_id": WORK_SHIFT_POSTING_ID,
        "employer_id": WORK_SHIFT_EMPLOYER_ID,
        "location_id": "town",
        "title": "档案登记员",
        "started_tick": 0,
        "ended_tick": None,
        "revision": 2,
    }]
    world["metadata"]["work_shift_terms"] = [{
        "posting_id": WORK_SHIFT_POSTING_ID,
        "duration_seconds": 3_600,
        "resource_id": WORK_SHIFT_RESOURCE_ID,
        "wage": "12.50",
    }]
    return world


def test_projection_is_conservative_and_does_not_mutate_world():
    world = state()
    before = deepcopy(world)
    view = project_player_state(world, "p")
    assert set(view["entities"]) == {"p", "n", "town"}
    assert view["entities"]["n"]["components"] == {"public_role": "商人"}
    assert set(view["resources"]) == {"mine"}
    assert view["knowledge"] == [{"claim": "听说有凶手"}]
    assert view["goals"] == [] and view["relations"] == []
    assert view["metadata"] == {"pack_id": "test"}
    assert "scheduled_events" not in view
    view["entities"]["p"]["components"]["health"] = 0
    assert world == before


def test_actor_owned_knowledge_requires_current_tick_and_non_author_visibility():
    world = state()
    world["current_tick"] = 10
    world["knowledge"] = [
        {
            "id": "author-only-observation", "owner_id": "p", "subject_id": "hidden",
            "kind": "observation", "claim": "author-only observation", "received_tick": 10,
            "valid_from_tick": 10, "visibility": {"policy": "author_only"},
        },
        {
            "id": "author-only-held", "holder_id": "p", "claim": "author-only held claim",
            "visibility": {"policy": "author_only"},
        },
        {"id": "future-received", "owner_id": "p", "claim": "future receipt", "received_tick": 11},
        {
            "id": "future-valid", "owner_id": "p", "claim": "future validity",
            "received_tick": 10, "valid_from_tick": 11,
        },
        {
            "id": "exact-tick", "owner_id": "p", "claim": "available at current tick",
            "received_tick": 10, "valid_from_tick": 10, "visibility": {"policy": "private"},
        },
        {"id": "legacy", "owner_id": "p", "claim": "legacy defaults remain available"},
    ]

    projected = project_player_state(world, "p")
    scene = action_scene(world, "p")

    assert {item["claim"] for item in projected["knowledge"]} == {
        "available at current tick", "legacy defaults remain available",
    }
    assert {item["claim"] for item in scene["knowledge"]} == {
        "available at current tick", "legacy defaults remain available",
    }
    assert "hidden" not in projected["entities"]
    assert "hidden" not in scene["entities"]


def test_daily_routine_is_not_visible_in_player_or_present_character_projection():
    world = state()
    world["entities"]["p"]["components"]["daily_routine"] = {"windows": [{"activity": "rest"}]}
    world["entities"]["n"]["components"]["daily_routine"] = {"windows": [{"activity": "work"}]}

    view = project_player_state(world, "p")

    assert "daily_routine" not in view["entities"]["p"]["components"]
    assert "daily_routine" not in view["entities"]["n"]["components"]


def test_study_skills_are_projected_for_only_the_selected_actor_and_fail_closed():
    world = state()
    world["entities"]["p"]["components"]["skills"] = {
        "skills": {"skill.care": {"level": "12.5", "experience": 3}}
    }
    world["entities"]["n"]["components"]["skills"] = {
        "skills": {"skill.secret": {"level": "99", "experience": 800}}
    }

    player_view = project_player_state(world, "p")
    assert player_view["own_study_skills"] == [{"skill_id": "skill.care", "level": "12.5"}]
    assert "skills" not in player_view["entities"]["n"]["components"]
    assert project_player_state(world, "n")["own_study_skills"] == [
        {"skill_id": "skill.secret", "level": "99"}
    ]

    world["entities"]["p"]["components"]["skills"] = {
        "skills": {"skill.care": {"level": "not-a-level"}}
    }
    assert project_player_state(world, "p")["own_study_skills"] == []
    world["entities"]["p"]["components"].pop("skills")
    assert project_player_state(world, "p")["own_study_skills"] == []


def test_action_scene_shows_only_the_actors_own_memberships():
    world = state()
    world["entities"]["guild"] = {
        "id": "guild", "kind": "organization", "name": "工会", "location_id": "remote",
        "components": {"hidden": True, "governance": {
            "charter": "私有章程", "members": ["p", "n"], "leader_id": "n",
        }},
    }
    world["entities"]["council"] = {
        "id": "council", "kind": "polity", "name": "议会", "location_id": "remote",
        "components": {"governance": {"members": ["n"], "leader_id": "n"}},
    }

    scene = action_scene(world, "p")
    assert scene["own_memberships"] == [{"organization_id": "guild", "name": "工会", "is_leader": False}]
    assert "guild" not in scene["entities"]
    assert "council" not in scene["entities"]
    scene["own_memberships"][0]["name"] = "另一个名字"
    assert world["entities"]["guild"]["name"] == "工会"


def test_vacant_leadership_projection_only_shows_the_compatible_visible_office_holder():
    proposer_id, holder_id, other_id, organization_id = (str(uuid4()) for _ in range(4))
    world = {
        "current_tick": 0,
        "entities": {
            proposer_id: {
                "id": proposer_id, "kind": "character", "name": "提议人", "status": "active", "components": {},
            },
            holder_id: {
                "id": holder_id, "kind": "character", "name": "现任职务持有人", "status": "active", "components": {},
            },
            other_id: {
                "id": other_id, "kind": "character", "name": "其他成员", "status": "active", "components": {},
            },
            organization_id: {
                "id": organization_id, "kind": "organization", "name": "议会", "status": "active",
                "components": {"governance": {
                    "members": [proposer_id, holder_id, other_id],
                    "leader_id": None,
                    "succession_policy": "election",
                    "offices": [{
                        "office_id": "office.leader", "title": "领导", "holder_id": holder_id,
                        "authority": ["organization.lead"],
                    }],
                }},
            },
        },
        "leadership_ballots": [],
    }

    membership = next(
        row for row in project_player_state(world, proposer_id)["own_memberships"]
        if row["organization_id"] == organization_id
    )

    assert membership["vacancy_ballot_nominees"] == [{
        "member_id": holder_id, "name": "现任职务持有人",
    }]
    assert membership["vacancy_ballot_existing_office_holder_id"] == holder_id

    world["entities"][holder_id]["components"]["hidden"] = True
    hidden_projection = next(
        row for row in project_player_state(world, proposer_id)["own_memberships"]
        if row["organization_id"] == organization_id
    )
    assert hidden_projection["vacancy_ballot_nominees"] == []
    assert "vacancy_ballot_existing_office_holder_id" not in hidden_projection


def test_only_incumbent_appointment_leader_sees_active_handover_candidates():
    world = state()
    world["entities"]["far"]["status"] = "active"
    world["entities"]["n"]["status"] = "retired"
    world["entities"]["guild"] = {
        "id": "guild", "kind": "organization", "name": "工会",
        "components": {"governance": {
            "members": ["p", "far", "n"], "leader_id": "p", "succession_policy": "appointment",
        }},
    }
    scene = action_scene(world, "p")
    assert scene["own_memberships"][0]["handover_candidates"] == [{"member_id": "far", "name": "远人"}]
    assert "far" not in scene["entities"]
    assert "handover_candidates" not in action_scene(world, "far")["own_memberships"][0]
    world["entities"]["guild"]["components"]["governance"]["succession_policy"] = "council"
    assert "handover_candidates" not in action_scene(world, "p")["own_memberships"][0]


def test_unknown_actor_cannot_receive_world():
    with pytest.raises(ProjectionError):
        project_player_state(state(), "unknown")


def test_shipment_projection_is_participant_only_and_receipt_requires_presence():
    world = state()
    world["entities"]["delivery"] = {
        "id": "delivery", "kind": "shipment", "name": "Private cargo", "location_id": "town",
        "components": {"shipment": {
            "sender_id": "p", "destination_owner_id": "n", "status": "awaiting_receipt",
            "departure_tick": 0, "arrival_tick": 60, "arrived_tick": 60,
            "origin_place_id": "remote", "destination_place_id": "town", "route_id": "private-route",
            "cargo": [{"resource_id": "grain", "quantity": "2", "transit_account_id": "confidential", "lots": ["lot"]}],
        }},
    }
    for viewer in ("p", "n", "hidden"):
        assert "delivery" not in project_player_state(world, viewer)["entities"]
    assert project_player_state(world, "hidden")["own_shipments"] == []

    sender, = project_player_state(world, "p")["own_shipments"]
    receiver, = project_player_state(world, "n")["own_shipments"]
    assert sender["role"] == "sender" and sender["can_receive"] is False
    assert sender["receiving_custody"] == "designated_holder"
    assert sender["destination_place_name"] == world["entities"]["town"]["name"]
    assert receiver["role"] == "receiver" and receiver["can_receive"] is True
    assert receiver["cargo"] == [{"resource_id": "grain", "quantity": "2"}]
    assert "route_id" not in receiver
    receiver["cargo"][0]["quantity"] = "0"
    assert world["entities"]["delivery"]["components"]["shipment"]["cargo"][0]["quantity"] == "2"
    world["entities"]["n"]["location_id"] = "remote"
    assert project_player_state(world, "n")["own_shipments"][0]["can_receive"] is False

    shipment = world["entities"]["delivery"]["components"]["shipment"]
    shipment["status"] = "delivered"
    assert project_player_state(world, "p")["own_shipments"][0]["cargo"][0]["delivered_quantity"] == "2"
    shipment.update(status="awaiting_receipt", lifecycle_version="expiry_v2")
    shipment["cargo"][0]["lots"] = [{"transit_lot_id": "spoiled-lot"}, {"transit_lot_id": "good-lot"}]
    shipment["spoiled_lots"] = [{"transit_lot_id": "spoiled-lot", "quantity": "0.5", "destruction_sink_account_id": "private"}]
    projected_cargo = project_player_state(world, "n")["own_shipments"][0]["cargo"]
    assert projected_cargo == [{"resource_id": "grain", "quantity": "2", "spoiled_quantity": "0.5", "remaining_quantity": "1.5"}]

    shipment.update(status="delivered", delivered_lots=[{"transit_lot_id": "good-lot", "quantity": "1.5", "private": "account"}])
    received_cargo = project_player_state(world, "n")["own_shipments"][0]["cargo"]
    assert received_cargo == [projected_cargo[0] | {"delivered_quantity": "1.5"}]
    assert project_player_state(world, "hidden")["own_shipments"] == []
    shipment["destination_owner_id"] = "town"
    assert project_player_state(world, "p")["own_shipments"][0]["receiving_custody"] == "place_depot"


def test_action_scene_shipment_options_expose_only_fixed_reachable_receivers():
    world = state()
    world["entities"]["p"]["components"]["inventory"] = {"lots": [
        {"account_id": "mine", "resource_id": "coin", "quantity": "3", "expiry_tick": 60},
        {"account_id": "mine", "resource_id": "coin", "quantity": "2", "expiry_tick": 61},
    ]}
    world["entities"]["remote"] = {"id": "remote", "kind": "place", "name": "Depot", "components": {"inventory": {}}}
    world["entities"]["hidden_depot"] = {"id": "hidden_depot", "kind": "place", "components": {"inventory": {}, "hidden": True}}
    world["entities"]["town"]["components"]["place"] = {"routes": [
        {"route_id": target, "from_place_id": "town", "to_place_id": target,
         "travel_ticks": 60, "is_open": True, "access_tags": []}
        for target in ("remote", "hidden_depot")
    ]}
    world["resources"].update({
        "delivery": {"owner_id": "remote", "resource_id": "coin", "balance": "900"},
        "private": {"owner_id": "far", "resource_id": "coin", "balance": "800"},
        "hidden": {"owner_id": "hidden_depot", "resource_id": "coin", "balance": "700"},
        "source": {"owner_id": "remote", "resource_id": "coin", "balance": "0", "account_type": "production_source"},
    })
    assert action_scene(world, "p")["shipment_dispatch_options"] == [{
        "route_id": "remote", "destination_name": "Depot", "source_owner_id": "p",
        "destination_owner_id": "remote", "resource_id": "coin", "source_account_id": "mine",
        "destination_account_id": "delivery", "available_quantity": "2",
    }]


def test_promise_projection_exposes_terms_only_to_participants():
    world = state()
    world["relationship_promises"] = [{
        "id": "promise", "promisor_id": "p", "promisee_id": "n", "private_text": "我明天还两袋粮食",
        "status": "active", "created_tick": 0, "deadline_tick": 86400, "deadline_schedule_id": "internal",
        "terms": {"kind": "return_item", "resource_id": "grain", "quantity": "2", "promisee_account_id": "private"},
    }]
    assert project_player_state(world, "far")["own_promises"] == []
    promisor, = project_player_state(world, "p")["own_promises"]
    promisee, = project_player_state(world, "n")["own_promises"]
    assert (promisor["role"], promisor["counterparty_id"]) == ("promisor", "n")
    assert (promisee["role"], promisee["counterparty_id"]) == ("promisee", "p")
    assert promisor["text"] == "我明天还两袋粮食"
    assert promisor["terms"] == {"kind": "return_item", "resource_id": "grain", "quantity": "2"}
    assert "deadline_schedule_id" not in promisee


def test_case_projection_requires_local_visible_anchor_and_owns_case_files():
    world = state()
    world["current_tick"] = 12
    definition = {"case_id": "theft", "title": "失窃", "brief": "库房少了一箱货", "routes": ["private"]}
    anchors = {
        "notice": {"location_id": "town"},
        "remote_notice": {"location_id": "remote"},
        "lost_notice": {"location_id": "town", "status": "destroyed"},
        "secret_notice": {"location_id": "town"},
    }
    for anchor_id, fields in anchors.items():
        world["entities"][anchor_id] = {"id": anchor_id, "kind": "item", **fields, "components": {
            "investigation_case": deepcopy(definition), "hidden": anchor_id == "secret_notice",
        }}
    world["knowledge"].append({"owner_id": "p", "subject_id": "secret_notice", "kind": "observation", "received_tick": 13})
    world["investigation_cases"] = [
        {"id": "mine", "owner_id": "p", "status": "open", "evidence_refs": ["receipt"], "routes": ["private"]},
        {"id": "theirs", "owner_id": "n", "status": "open"},
        {"id": "closed", "owner_id": "p", "status": "closed", "outcome": "supported",
         "matched_route_ids": ["private-source-name"], "definition_hash": "author-only"},
    ]
    view = project_player_state(world, "p")
    assert view["available_case_anchors"] == [
        {"anchor_id": "notice", "case_id": "theft", "title": "失窃", "brief": "库房少了一箱货"},
    ]
    assert view["case_files"] == [
        {"id": "mine", "owner_id": "p", "status": "open", "evidence_refs": ["receipt"]},
        {"id": "closed", "owner_id": "p", "status": "closed", "outcome": "supported"},
    ]
    view["case_files"][0]["evidence_refs"].clear()
    assert world["investigation_cases"][0]["evidence_refs"] == ["receipt"]
    world["current_tick"] = 13
    assert [item["anchor_id"] for item in project_player_state(world, "p")["available_case_anchors"]] == ["notice", "secret_notice"]
    assert "investigation_case" not in project_player_state(world, "p")["entities"]["secret_notice"]["components"]
    assert [item["id"] for item in project_player_state(world, "n")["case_files"]] == ["theirs"]
    world["entities"]["p"]["status"] = "retired"
    assert project_player_state(world, "p")["available_case_anchors"] == []


def test_action_scene_names_public_route_without_revealing_hidden_destinations():
    world = state()
    world["entities"]["road"] = {"id": "road", "kind": "place", "name": "Crossroads", "components": {}}
    world["entities"]["vault"] = {"id": "vault", "kind": "place", "name": "Hidden Vault", "components": {"hidden": True}}
    world["entities"]["town"]["components"]["place"] = {"routes": [
        {"route_id": destination, "from_place_id": "town", "to_place_id": destination,
         "travel_ticks": 60, "is_open": True, "access_tags": []}
        for destination in ("road", "vault")
    ]}
    scene = action_scene(world, "p")
    assert scene["routes"] == [{"route_id": "road", "to_place_id": "road", "travel_ticks": 60, "destination_name": "Crossroads"}]
    assert "vault" not in scene["entities"]

    world["knowledge"].append({"owner_id": "p", "subject_id": "vault", "kind": "observation", "received_tick": 60})
    assert "vault" not in project_player_state(world, "p")["entities"]
    world["current_tick"] = 60
    assert "vault" in project_player_state(world, "p")["entities"]
    assert "vault" not in project_player_state(world, "n")["entities"]
    assert [route["to_place_id"] for route in action_scene(world, "p")["routes"]] == ["road", "vault"]


def test_action_scene_reveals_only_the_actors_visible_active_combat_opponent():
    world = state()
    world["entities"]["p"]["status"] = world["entities"]["n"]["status"] = "active"
    encounter = {"encounter_id": "e", "status": "active", "location_id": "town"}
    world["entities"]["p"]["components"]["combat_encounter"] = {**encounter, "opponent_id": "n"}
    world["entities"]["n"]["components"]["combat_encounter"] = {**encounter, "opponent_id": "p"}

    assert action_scene(world, "p")["active_combat"] == {"opponent_id": "n", "opponent_name": "商人"}
    assert "combat_encounter" not in action_scene(world, "p")["entities"]["n"]["components"]
    world["entities"]["n"]["components"]["hidden"] = True
    assert "active_combat" not in action_scene(world, "p")
    world["entities"]["n"]["components"].pop("hidden")
    world["entities"]["n"]["components"]["combat_encounter"]["status"] = "retreated"
    assert "active_combat" not in action_scene(world, "p")


def test_investigation_action_scene_includes_only_actor_authorized_typed_records():
    player_id, npc_id, subject_id, place_id = (uuid4() for _ in range(4))
    observation_id, rumor_id, private_id = (uuid4() for _ in range(3))
    place = WorldEntity(id=place_id, kind="place", name="Old Bridge")
    player = WorldEntity(id=player_id, kind="character", name="Player", location_id=place_id)
    npc = WorldEntity(id=npc_id, kind="character", name="Neighbor", location_id=place_id)
    subject = WorldEntity(id=subject_id, kind="character", name="Witness", location_id=place_id)
    state = WorldState(
        player_id=player_id,
        entities={entity.id: entity for entity in (place, player, npc, subject)},
        knowledge=[
            {
                "id": observation_id, "owner_id": player_id, "subject_id": subject_id,
                "claim": "The witness was at the old bridge.", "kind": "observation",
                "source_event_id": uuid4(),
                "fact": {"predicate": "entity_location", "value": place_id, "effective_tick": 60},
            },
            {
                "id": rumor_id, "owner_id": player_id, "subject_id": subject_id,
                "claim": "A neighbor says the witness was elsewhere.", "kind": "rumor",
                "source_event_id": uuid4(),
                "fact": {"predicate": "entity_location", "value": uuid4(), "effective_tick": 60},
            },
            {
                "id": private_id, "holder_id": npc_id, "subject_id": subject_id,
                "claim": "A private source claim.", "kind": "rumor",
                "source_event_id": uuid4(),
                "fact": {"predicate": "entity_location", "value": place_id, "effective_tick": 60},
            },
        ],
    )

    scene = action_scene(state.model_dump(mode="json"), str(player_id), authoritative=state)

    records = scene["investigation_records"]
    assert [record["record_id"] for record in records] == [str(observation_id), str(rumor_id)]
    assert all("source_event_id" not in record and "source_refs" not in record for record in records)
    assert str(private_id) not in str(records)


def test_treatment_scene_shows_only_own_untreated_injuries_and_usable_medicine():
    world = state()
    player = world["entities"]["p"]
    player["status"] = "active"
    injury_id, healed_id, other_injury_id = (str(uuid4()) for _ in range(3))
    player["components"]["health"] = {"current": 70, "maximum": 100, "injuries": [
        {"injury_id": injury_id, "injury_type": "scratch", "severity": 10, "started_tick": 0},
        {"injury_id": healed_id, "injury_type": "scar", "severity": 5, "started_tick": 0, "treated": True},
    ]}
    world["entities"]["n"]["components"]["health"] = {"current": 80, "maximum": 100, "injuries": [
        {"injury_id": other_injury_id, "injury_type": "private", "severity": 8, "started_tick": 0},
    ]}
    active, expired = str(uuid4()), str(uuid4())
    world["resources"][active] = {"owner_id": "p", "resource_id": "core.medicine", "balance": "1"}
    world["resources"][expired] = {"owner_id": "p", "resource_id": "core.herbs", "balance": "1"}
    player["components"]["inventory"] = {"lots": [
        {"lot_id": str(uuid4()), "account_id": active, "resource_id": "core.medicine", "quantity": "1"},
        {"lot_id": str(uuid4()), "account_id": expired, "resource_id": "core.herbs", "quantity": "1", "expiry_tick": 1},
    ]}
    world["current_tick"] = 1
    scene = action_scene(world, "p")
    assert scene["self_treatment"] == {
        "target_id": "p", "injuries": [{"injury_id": injury_id, "injury_type": "scratch", "severity": 10}],
        "medicine_resource_ids": ["core.medicine"],
    }
    assert other_injury_id not in str(scene)


def test_own_active_assignment_projects_only_registered_paid_shift_terms():
    world = work_shift_state()

    view = project_player_state(world, "p")

    assignment, = view["own_work_assignments"]
    assert assignment["work_shift"] == {
        "duration_seconds": 3_600,
        "resource_id": WORK_SHIFT_RESOURCE_ID,
        "wage": "12.50",
        "can_work_shift": True,
    }
    assert WORK_SHIFT_EMPLOYER_ACCOUNT_ID not in str(assignment)
    assert WORK_SHIFT_WORKER_ACCOUNT_ID not in str(assignment)
    assert "balance" not in assignment["work_shift"]
    assert "account_id" not in assignment["work_shift"]


@pytest.mark.parametrize(
    ("change", "expected"),
    [
        (lambda world: world["entities"]["p"].update(status="retired"), False),
        (lambda world: world["entities"][WORK_SHIFT_EMPLOYER_ID].update(status="dissolved"), False),
        (lambda world: world["entities"]["p"].update(location_id="remote"), False),
        (lambda world: world["entities"]["town"].update(status="closed"), False),
        (lambda world: world["resources"][WORK_SHIFT_EMPLOYER_ACCOUNT_ID].update(balance="1"), False),
        (lambda world: world["resources"].pop(WORK_SHIFT_WORKER_ACCOUNT_ID), False),
        (lambda world: world["work_postings"][0].update(status="closed"), False),
        (lambda world: world["work_postings"][0].update(valid_until_tick=3_000), False),
    ],
)
def test_can_work_shift_fails_closed_when_required_current_conditions_are_missing(change, expected):
    world = work_shift_state()
    change(world)

    assignment, = project_player_state(world, "p")["own_work_assignments"]

    assert assignment["work_shift"]["can_work_shift"] is expected


def test_shift_terms_are_omitted_for_ended_or_unregistered_assignments():
    world = work_shift_state()
    world["work_assignments"].append({
        **world["work_assignments"][0],
        "assignment_id": "91000000-0000-4000-8000-000000000019",
        "ended_tick": 300,
    })
    world["work_assignments"][0]["posting_id"] = "92000000-0000-4000-8000-000000000020"

    assignments = project_player_state(world, "p")["own_work_assignments"]

    assert all("work_shift" not in item for item in assignments)


def test_malformed_or_duplicate_shift_registrations_are_not_projected():
    world = work_shift_state()
    world["metadata"]["work_shift_terms"].append(dict(world["metadata"]["work_shift_terms"][0]))

    assignment, = project_player_state(world, "p")["own_work_assignments"]

    assert "work_shift" not in assignment


def test_action_scene_lists_only_local_visible_historical_artifact_targets():
    place_id, actor_id, item_id, subject_id, hidden_id, destroyed_id = (uuid4() for _ in range(6))
    artifact = HistoricalEvidenceArtifact(
        evidence_id=uuid4(), subject_id=subject_id,
        claim="A concealed archival statement.",
        fact={"predicate": "entity_status", "value": "active", "effective_tick": -3600},
        provenance=[{"kind": "pack", "reference_id": "pack.test.archive"}],
    )
    hidden_artifact = artifact.model_copy(update={"evidence_id": uuid4()})
    destroyed_artifact = artifact.model_copy(update={"evidence_id": uuid4()})
    state_value = WorldState(
        player_id=actor_id,
        entities={
            place_id: WorldEntity(id=place_id, kind="place", name="Archive"),
            actor_id: WorldEntity(id=actor_id, kind="character", name="Player", location_id=place_id),
            item_id: WorldEntity(
                id=item_id, kind="item", name="Old register", location_id=place_id,
                components={"historical_evidence": artifact.model_dump(mode="json")},
            ),
            hidden_id: WorldEntity(
                id=hidden_id, kind="item", name="Hidden register", location_id=place_id,
                components={"hidden": True, "historical_evidence": hidden_artifact.model_dump(mode="json")},
            ),
            destroyed_id: WorldEntity(
                id=destroyed_id, kind="item", name="Destroyed register", location_id=place_id,
                status="destroyed", components={"historical_evidence": destroyed_artifact.model_dump(mode="json")},
            ),
            subject_id: WorldEntity(id=subject_id, kind="character", name="Subject"),
        },
    )

    scene = action_scene(
        state_value.model_dump(mode="json"), str(actor_id), authoritative=state_value,
    )

    assert scene["destroyable_sources"] == [{"target_id": str(item_id), "name": "Old register"}]
    assert artifact.claim not in str(scene)
    assert str(artifact.evidence_id) not in str(scene)


def test_history_requires_audience_safe_payload_not_just_visible_event():
    event = {"event_id": "e", "type": "speech", "payload": {"secret": "truth"}, "visibility": {"policy": "public"}}
    assert project_event(event, "p") is None
    event["audience_views"] = {"public": {"said": "我不知道"}}
    assert project_event(event, "p")["payload"] == {"said": "我不知道"}
    event["visibility"] = {"policy": "participants", "subject_ids": ["n"]}
    assert project_event(event, "p") is None
