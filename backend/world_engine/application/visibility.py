"""Conservative player projections, shared by UI and model-context construction.

Unknown fields are private by default. Author data is never sent to a model and
then merely hidden in the UI. Callers must authorize actor control separately.
"""

from __future__ import annotations

from collections import Counter
from copy import deepcopy
from decimal import Decimal, localcontext
from typing import Any
from uuid import UUID

from world_engine.agents import AgentKnowledge, AgentVisibleEntity
from world_engine.domain.components import (
    GovernanceComponent, KnowledgeRecord, LifeComponent, MarketComponent, SkillsComponent,
)
from world_engine.domain.errors import InvalidEvent
from world_engine.domain.leadership_ballots import vacant_leadership_office_holder
from world_engine.domain.models import CalendarConfig, WorldState
from world_engine.domain.story_nodes import STORY_NODE_HANDLER
from world_engine.domain.family_kinship import inspect_kinship
from world_engine.domain.promise_trust import PROMISE_TRUST_POLICY_ID
from world_engine.domain.promise_trust_state import PromiseTrustProjection
from world_engine.domain.relationship_disclosure_events import parse_relationship_disclosure_event
from world_engine.domain.combat_truce import CombatTruceChangedEvent, CombatTruceOffer
from world_engine.domain.cultivation_breakthrough_events import CultivationBreakthroughResolvedEvent
from world_engine.domain.relationship_invitations import RelationshipInvitation
from world_engine.domain.relationship_reflections import MeetingReflection
from world_engine.domain.relationship_partnerships import RelationshipPartnership
from world_engine.domain.shipment_relief_events import ShipmentReliefDistributedEvent
from world_engine.domain.private_gifts import PrivateGiftChangedEvent, PrivateGiftOffer
from world_engine.domain.travel_journey_events import TravelJourneyChangedEvent
from world_engine.domain.travel_journeys import TravelJourney
from world_engine.domain.waypoint_opportunity_events import (
    WAYPOINT_OPPORTUNITY_DECISION_RECORDED_EVENT_TYPE, WaypointOpportunityDecisionRecordedEvent,
)
from world_engine.domain.work_shift import WorkShiftTerms

PUBLIC_COMPONENTS = frozenset({"public_description", "appearance", "public_role"})
SELF_COMPONENTS = PUBLIC_COMPONENTS | frozenset(
    {"health", "needs", "attributes", "capabilities", "progression", "known_places"}
)


class ProjectionError(ValueError):
    pass


def meeting_local_time(tick: int, current_tick: int) -> dict[str, int | str]:
    day, seconds = divmod(tick, 86_400)
    hour, remaining = divmod(seconds, 3600)
    minute, second = divmod(remaining, 60)
    return {"day_offset_from_now": day - current_tick // 86_400,
            "clock": f"{hour:02d}:{minute:02d}:{second:02d}"}


def present_dialogue_targets(
    state: WorldState, observation: AgentKnowledge, visible_entities: dict[str, Any],
) -> dict[str, AgentVisibleEntity]:
    if state.entities[observation.actor_id].status in {"dead", "retired"}:
        return {}
    return {
        str(entity.id): entity
        for entity in observation.visible_entities
        if entity.kind in {"character", "person"} and str(entity.id) in visible_entities
    }


def _owned(item: dict[str, Any], actor_id: str) -> bool:
    return item.get("owner_id") == actor_id or item.get("holder_id") == actor_id


def _owned_knowledge_is_eligible(item: dict[str, Any], actor_id: str, current_tick: int) -> bool:
    """Keep an actor's claims only after receipt and outside author-only scope.

    Imported legacy rows may omit visibility or tick fields; those retain their
    historical private-owner behavior and default to tick zero.
    """
    if not _owned(item, actor_id):
        return False
    visibility = item.get("visibility")
    if isinstance(visibility, dict) and visibility.get("policy") == "author_only":
        return False
    try:
        received_tick = int(item.get("received_tick", 0))
        valid_from = item.get("valid_from_tick")
        valid_from_tick = None if valid_from is None else int(valid_from)
    except (TypeError, ValueError, OverflowError):
        return False
    return received_tick <= current_tick and (valid_from_tick is None or valid_from_tick <= current_tick)


def _visible(item: dict[str, Any], actor_id: str) -> bool:
    visibility = item.get("visibility", {})
    if not isinstance(visibility, dict):
        return False
    return visibility.get("policy") == "public" or actor_id in visibility.get("subject_ids", [])


def _own_study_skills(actor: dict[str, Any]) -> list[dict[str, str]]:
    components = actor.get("components")
    if not isinstance(components, dict) or components.get("skills") is None:
        return []
    try:
        skills = SkillsComponent.model_validate(components["skills"])
    except Exception:
        return []
    return [
        {"skill_id": skill_id, "level": format(skill.level, "f")}
        for skill_id, skill in sorted(skills.skills.items())
    ]


def _private_gift_offer_view(offer: PrivateGiftOffer) -> dict[str, Any]:
    """Serialize only participant-safe fields from one validated gift offer."""
    return {
        "gift_id": str(offer.gift_id),
        "giver_id": str(offer.giver_id),
        "recipient_id": str(offer.recipient_id),
        "place_id": str(offer.place_id),
        "resource_id": offer.resource_id,
        "quantity": format(offer.quantity, "f"),
        "offered_tick": str(offer.offered_tick),
        "expires_at_tick": str(offer.expires_at_tick),
        "revision": offer.revision,
        "status": offer.status,
        "resolved_tick": str(offer.resolved_tick) if offer.resolved_tick is not None else None,
    }


def _own_private_gifts(
    state: dict[str, Any], actor_id: str, entities: dict[str, Any], current_tick: int,
) -> list[dict[str, Any]]:
    gifts: list[dict[str, Any]] = []
    raw_player_id = state.get("player_id")
    canonical_player_id = str(raw_player_id) if raw_player_id is not None else None
    actor = entities.get(actor_id)
    for raw in state.get("private_gifts", []):
        try:
            offer = PrivateGiftOffer.model_validate(raw)
        except (TypeError, ValueError):
            continue
        giver_id = str(offer.giver_id)
        recipient_id = str(offer.recipient_id)
        if actor_id not in {giver_id, recipient_id}:
            continue
        pending = offer.status == "pending"
        giver = entities.get(giver_id)
        recipient = entities.get(recipient_id)
        at_offer_place = (
            isinstance(giver, dict) and isinstance(recipient, dict)
            and giver.get("kind") in {"character", "person"}
            and recipient.get("kind") in {"character", "person"}
            and giver.get("status", "active") == "active"
            and recipient.get("status", "active") == "active"
            and giver.get("location_id") == str(offer.place_id)
            and recipient.get("location_id") == str(offer.place_id)
            and isinstance((place := entities.get(str(offer.place_id))), dict)
            and place.get("kind") in {"place", "location"}
            and place.get("status", "active") == "active"
        )
        entry = _private_gift_offer_view(offer)
        entry.update({
            "expired": pending and offer.expires_at_tick <= current_tick,
            "can_accept": (
                pending and actor_id == recipient_id and offer.expires_at_tick > current_tick
                and isinstance(actor, dict) and actor.get("status", "active") == "active" and at_offer_place
            ),
            "can_decline": (
                pending and actor_id == recipient_id
                and isinstance(actor, dict) and actor.get("status", "active") == "active"
            ),
            "can_withdraw": (
                pending and actor_id == giver_id
                and isinstance(actor, dict) and actor.get("status", "active") == "active"
            ),
            "can_request_reply": (
                pending and offer.expires_at_tick > current_tick and actor_id == giver_id
                and recipient_id != canonical_player_id and at_offer_place
            ),
        })
        gifts.append(entry)
    return gifts


def project_player_state(state: dict[str, Any], actor_id: str) -> dict[str, Any]:
    """Create an isolated allowlisted view; never mutate the authoritative state.

    Private records have to be converted into observer-owned knowledge by the
    observation system. Knowing a character's name does not reveal their present
    location, inventory, private goals or mental state.
    """
    entities = state.get("entities", {})
    actor = entities.get(actor_id)
    if not actor or actor.get("kind") != "character":
        raise ProjectionError("Actor is not a character in this world.")
    location = actor.get("location_id")
    known_places = set(actor.get("components", {}).get("known_places", []))
    if location:
        known_places.add(location)
    tick = int(state.get("current_tick", 0))
    observed_entities = {
        item.get("subject_id") for item in state.get("knowledge", [])
        if _owned_knowledge_is_eligible(item, actor_id, tick) and item.get("kind") == "observation"
    }
    known_places.update(observed_entities)

    projected_entities: dict[str, Any] = {}
    for entity_id, entity in entities.items():
        if entity.get("kind") == "shipment":
            continue
        own = entity_id == actor_id
        place = entity.get("kind") in {"place", "location"} and entity_id in known_places
        present = bool(location) and entity.get("location_id") == location
        # Hidden entities require an observation/reveal event before inclusion.
        if not own and entity_id not in observed_entities and entity.get("components", {}).get("hidden", False):
            continue
        if not (own or place or present):
            continue
        allowed = SELF_COMPONENTS if own else PUBLIC_COMPONENTS
        entry = {key: deepcopy(entity[key]) for key in ("id", "kind", "name") if key in entity}
        if own or present or entity_id == location:
            entry.update({key: deepcopy(entity[key]) for key in ("location_id", "status") if key in entity})
        entry["components"] = {
            key: deepcopy(value)
            for key, value in entity.get("components", {}).items()
            if key in allowed
        }
        capabilities = entry["components"].get("capabilities")
        if isinstance(capabilities, dict):
            capabilities.pop("combat_death_protection", None)
        projected_entities[entity_id] = entry

    knowledge = [
        {key: deepcopy(item[key]) for key in (
            "id", "claim", "summary", "confidence", "source_refs", "received_tick", "kind"
        ) if key in item}
        for item in state.get("knowledge", [])
        if _owned_knowledge_is_eligible(item, actor_id, tick) and not item.get("relationship_disclosure")
    ]
    goals = [
        {key: deepcopy(item[key]) for key in (
            "id", "title", "description", "status", "deadline", "progress", "owner_id"
        ) if key in item}
        for item in state.get("goals", [])
        if _owned(item, actor_id)
    ]
    relations = [
        {key: deepcopy(item[key]) for key in (
            "id", "from_id", "to_id", "dimensions", "label", "evidence_refs"
        ) if key in item}
        for item in state.get("relations", [])
        if item.get("from_id") == actor_id
    ]
    resources = {
        account_id: {key: deepcopy(account[key]) for key in (
            "resource_id", "balance", "owner_id", "name"
        ) if key in account}
        for account_id, account in state.get("resources", {}).items()
        if account.get("owner_id") == actor_id and account.get("account_type", "holder") == "holder"
    }
    memberships = []
    open_ballot_organizations = {
        ballot["organization_id"] for ballot in state.get("leadership_ballots", [])
        if ballot["status"] == "open"
    }
    for entity_id, entity in sorted(entities.items()):
        if entity.get("kind") not in {"organization", "polity", "faction", "company", "government"} or entity.get("status", "active") != "active":
            continue
        governance = entity.get("components", {}).get("governance")
        if not isinstance(governance, dict) or actor_id not in governance.get("members", []):
            continue
        is_leader = governance.get("leader_id") == actor_id
        membership = {
            "organization_id": entity_id,
            "name": entity.get("name"),
            "is_leader": is_leader,
        }
        policy = governance.get("succession_policy")
        try:
            typed_governance = GovernanceComponent.model_validate(governance)
            office_holder_id = vacant_leadership_office_holder(typed_governance)
        except (TypeError, ValueError):
            typed_governance = None
            office_holder_id = None
        vacancy = (
            typed_governance is not None
            and actor.get("status", "active") == "active"
            and typed_governance.succession_policy in {"election", "council"}
            and entity_id not in open_ballot_organizations
            and (typed_governance.succession_policy == "election" or any(
                str(office.holder_id) == actor_id and "authority.succession_vote" in office.authority
                for office in typed_governance.offices
            ))
        )
        if vacancy:
            membership["ballot_policy"] = typed_governance.succession_policy
            candidates = [
                {"member_id": str(member_id), "name": member.get("name")}
                for member_id in sorted(map(str, typed_governance.members))
                if (office_holder_id is None or member_id == str(office_holder_id))
                and (member := entities.get(member_id)) is not None
                and member.get("kind") in {"character", "person"}
                and member.get("status", "active") == "active"
                and (member_id == actor_id or not member.get("components", {}).get("hidden"))
            ]
            membership["vacancy_ballot_nominees"] = candidates
            if office_holder_id is not None and any(
                candidate["member_id"] == str(office_holder_id) for candidate in candidates
            ):
                membership["vacancy_ballot_existing_office_holder_id"] = str(office_holder_id)
        if is_leader and policy in {"appointment", "election", "council"}:
            candidates = []
            for member_id in sorted(governance["members"]):
                member = entities.get(member_id)
                if (
                    member_id != actor_id and member is not None
                    and member.get("kind") in {"character", "person"} and member.get("status", "active") == "active"
                ):
                    candidates.append({"member_id": member_id, "name": member.get("name")})
            if policy == "appointment":
                membership["handover_candidates"] = candidates
            else:
                membership["ballot_policy"] = policy
                membership["ballot_nominees"] = candidates
        memberships.append(membership)
    ballots = []
    for ballot in state.get("leadership_ballots", []):
        incumbent = ballot["incumbent_id"] == actor_id
        vacancy_proposer = ballot.get("vacancy_proposer_id") == actor_id
        voter = actor_id in ballot["eligible_voter_ids"]
        if not incumbent and not voter and ballot["nominee_id"] != actor_id:
            continue
        own_vote = next((row["choice"] for row in ballot["votes"] if row["voter_id"] == actor_id), None)
        waiting_voters = []
        if (incumbent or vacancy_proposer) and ballot["status"] == "open":
            cast_ids = {row["voter_id"] for row in ballot["votes"]}
            waiting_voters = [
                {"voter_id": voter_id, "name": entities[voter_id].get("name")}
                for voter_id in ballot["eligible_voter_ids"]
                if voter_id not in cast_ids and voter_id in entities and voter_id not in {actor_id, state.get("player_id")}
                and entities[voter_id].get("status", "active") == "active"
            ]
        ballots.append({
            "ballot_id": ballot["ballot_id"], "organization_id": ballot["organization_id"],
            "nominee_id": ballot["nominee_id"], "nominee_name": entities.get(ballot["nominee_id"], {}).get("name"),
            "status": ballot["status"], "policy": ballot["policy"], "revision": ballot["revision"],
            "is_incumbent": incumbent, "is_voter": voter, "own_vote": own_vote,
            **({"is_vacancy_proposer": True} if vacancy_proposer else {}),
            "waiting_voters": waiting_voters,
        })
    led_orgs = {row["organization_id"] for row in memberships if row["is_leader"]}
    safe_conduct = []
    for treaty in state.get("safe_conduct_treaties", []):
        proposer_org = treaty["proposer_organization_id"]
        recipient_org = treaty["recipient_organization_id"]
        if actor_id not in {treaty["proposer_actor_id"], treaty["recipient_actor_id"]} and not led_orgs.intersection({proposer_org, recipient_org}):
            continue
        endpoints = {
            key: entity.get("name") if not entity.get("components", {}).get("hidden") else None
            for key, entity in (
                ("from_place_name", entities.get(treaty["from_place_id"], {})),
                ("to_place_name", entities.get(treaty["to_place_id"], {})),
            )
        }
        deadline = treaty.get("counter_expires_at_tick") if treaty["status"] == "countered" else treaty["expires_at_tick"]
        safe_conduct.append({
            **{key: treaty[key] for key in (
                "treaty_id", "proposer_organization_id", "recipient_organization_id", "from_place_id", "to_place_id",
                "route_id", "expires_at_tick", "status", "revision",
            )},
            "proposer_name": entities.get(proposer_org, {}).get("name"),
            "recipient_name": entities.get(recipient_org, {}).get("name"),
            **endpoints,
            "counter_expires_at_tick": treaty.get("counter_expires_at_tick"),
            "expired": tick >= int(deadline),
            "represents_proposer": proposer_org in led_orgs,
            "represents_recipient": recipient_org in led_orgs,
            "can_request_response": (actor_id == treaty["proposer_actor_id"] and proposer_org in led_orgs
                                     and treaty["status"] == "pending" and tick < int(treaty["expires_at_tick"])),
        })
    correction_ticks = {item["case_file_id"]: item["corrected_tick"]
                        for item in state.get("public_accusation_corrections", []) if item["actor_id"] == actor_id}
    own_accusation_repair_offers = [
        {key: deepcopy(offer[key]) for key in (
            "offer_id", "revision", "accuser_id", "suspect_id", "statement", "status",
            "offered_tick", "updated_tick", "last_operation",
        ) if key in offer}
        for offer in state.get("accusation_repair_offers", [])
        if actor_id in (offer.get("accuser_id"), offer.get("suspect_id"))
    ]
    result = {
        "entities": projected_entities,
        "own_study_skills": _own_study_skills(actor),
        "resources": resources,
        "knowledge": knowledge,
        "goals": goals,
        "relations": relations,
        "own_memberships": memberships,
        "own_leadership_ballots": ballots,
        "own_safe_conduct_treaties": safe_conduct,
        "market_offers": _local_market_offers(entities, actor_id, location, observed_entities, tick),
        "own_shipments": _participant_shipments(entities, actor_id, actor),
        "own_promises": _participant_promises(state, actor_id),
        "own_combat_truces": [
            own for value in state.get("combat_truce_offers", [])
            if (own := _combat_truce_for_actor(value, actor_id)) is not None
        ][-12:],
        "own_relationship_invitations": [
            public for value in state.get("relationship_invitations", [])
            if (public := _invitation_for_actor(value, actor_id, entities=entities)) is not None
        ],
        "own_meeting_reflections": [
            own for value in state.get("relationship_reflections", [])
            if (own := _meeting_reflection_for_actor(value, actor_id)) is not None
        ],
        "own_relationship_partnerships": [
            own for value in state.get("relationship_partnerships", [])
            if (own := _partnership_for_actor(
                value, actor_id, entities=entities, current_tick=tick,
                calendar=state.get("calendar"), current_player_id=state.get("player_id"), include_actions=True,
            )) is not None
        ],
        "own_private_gifts": _own_private_gifts(state, actor_id, entities, tick),
        **({"own_accusation_repair_offers": own_accusation_repair_offers} if own_accusation_repair_offers else {}),
        "own_relationship_disclosures": _own_relationship_disclosures(state, actor_id, tick),
        "relationship_disclosure_options": _relationship_disclosure_options(
            state, actor_id, projected_entities, tick,
        ),
        "own_active_travel": next((
            own for value in state.get("travel_journeys", [])
            if (own := _travel_for_actor(value, actor_id)) is not None and own["status"] == "in_transit"
        ), None),
        "available_case_anchors": _case_anchors(entities, actor, observed_entities),
        "case_files": [
            {key: deepcopy(case[key]) for key in (
                "id", "case_id", "anchor_id", "owner_id", "title", "brief", "status", "revision",
                "opened_tick", "updated_tick", "evidence_refs", "closed_tick", "outcome", "suspect_id",
                "public_accusation",
            ) if key in case} | ({"public_accusation_corrected_tick": correction_ticks[case["id"]]}
                                 if case["id"] in correction_ticks else {})
            for case in state.get("investigation_cases", []) if case.get("owner_id") == actor_id
        ],
        "player_id": actor_id,
        "calendar": deepcopy(state.get("calendar", {})),
        # Author metadata, event queues and other actors' plans stay private.
        "metadata": {key: deepcopy(state.get("metadata", {})[key]) for key in (
            "pack_id", "public_title", "public_description"
        ) if key in state.get("metadata", {})},
    }
    if state.get("story_milestones"):
        public_nodes = {item["id"]: item["payload"] for item in state.get("scheduled_events", [])
                        if item["handler_id"] == STORY_NODE_HANDLER and item["payload"].get("visibility") == "public"}
        public_nodes.update({item["reaction_id"]: item["node"] for item in state.get("story_reactions", [])
                             if item["node"]["visibility"] == "public"})
        chronicle = []
        for record in state["story_milestones"]:
            node = public_nodes.get(record["schedule_id"])
            if node is None:
                continue
            outcomes = [node["fallback"], *(variant["outcome"] for variant in node.get("variants", ()))]
            outcome = next((row for row in outcomes if row["key"] == record["outcome"]), None)
            if outcome is not None:
                chronicle.append({"record_id": record["schedule_id"], "title": node["title"],
                                  "summary": outcome["summary"], "resolved_tick": record["resolved_tick"]})
        if chronicle:
            result["story_milestones"] = chronicle[-24:]
    if state.get("kinship_links"):
        inspection = inspect_kinship(state["kinship_links"], UUID(actor_id), at_tick=tick)
        if inspection.parents or inspection.children or inspection.siblings:
            known_family = {str(row.parent_id) for row in inspection.parents} | {
                str(row.child_id) for row in inspection.children
            } | {str(row.sibling_id) for row in inspection.siblings}
            people = {
                ident: {"name": entities[ident].get("name")}
                for ident in sorted(known_family) if ident in entities
                and (not entities[ident].get("components", {}).get("hidden") or ident in observed_entities)
            }
            result["family"] = inspection.model_dump(mode="json") | {"people": people}
    if state.get("work_postings") or state.get("work_assignments"):
        assignments = state.get("work_assignments", [])
        filled = Counter(item["posting_id"] for item in assignments if item.get("ended_tick") is None)
        shift_terms = _registered_shift_terms(state)
        result["own_work_assignments"] = [
            _project_work_assignment(
                item, actor_id=actor_id, actor=actor, entities=entities, resources=state.get("resources", {}),
                postings=state.get("work_postings", []), current_tick=tick, terms_by_posting=shift_terms,
            )
            for item in assignments if item["actor_id"] == actor_id
        ]
        result["local_work_postings"] = [
            {key: deepcopy(posting[key]) for key in ("posting_id", "title", "required_skill", "revision")}
            for posting in state.get("work_postings", [])
            if posting["location_id"] == location and location in projected_entities and posting["status"] == "open"
            and posting["valid_from_tick"] <= tick
            and (posting.get("valid_until_tick") is None or tick < posting["valid_until_tick"])
            and filled[posting["posting_id"]] < posting["slots"]
            and entities.get(posting["employer_id"], {}).get("status") == "active"
            and (not entities[posting["employer_id"]].get("components", {}).get("hidden")
                 or posting["employer_id"] in projected_entities)
        ]
    return result


def _registered_shift_terms(state: dict[str, Any]) -> dict[str, WorkShiftTerms]:
    """Fail closed when authored per-posting work terms are absent or malformed."""

    metadata = state.get("metadata", {})
    rows = metadata.get("work_shift_terms") if isinstance(metadata, dict) else None
    if rows is None:
        return {}
    if not isinstance(rows, list):
        return {}
    posting_counts = Counter(
        posting.get("posting_id") for posting in state.get("work_postings", [])
        if isinstance(posting, dict) and isinstance(posting.get("posting_id"), str)
    )
    registered: dict[str, WorkShiftTerms] = {}
    try:
        for row in rows:
            terms = WorkShiftTerms.model_validate(row)
            posting_id = str(terms.posting_id)
            if posting_counts[posting_id] != 1 or posting_id in registered:
                return {}
            registered[posting_id] = terms
    except Exception:
        return {}
    return registered


def _project_work_assignment(
    assignment: dict[str, Any],
    *,
    actor_id: str,
    actor: dict[str, Any],
    entities: dict[str, Any],
    resources: dict[str, Any],
    postings: list[dict[str, Any]],
    current_tick: int,
    terms_by_posting: dict[str, WorkShiftTerms],
) -> dict[str, Any]:
    projected = {
        key: deepcopy(assignment[key])
        for key in ("assignment_id", "posting_id", "title", "started_tick", "ended_tick", "revision")
        if key in assignment
    }
    if assignment.get("ended_tick") is not None:
        return projected

    posting_id = assignment.get("posting_id")
    terms = terms_by_posting.get(posting_id) if isinstance(posting_id, str) else None
    if terms is None:
        return projected
    matching = [
        posting for posting in postings
        if posting.get("posting_id") == posting_id
        and posting.get("employer_id") == assignment.get("employer_id")
        and posting.get("location_id") == assignment.get("location_id")
    ]
    if len(matching) != 1:
        return projected
    posting = matching[0]
    projected["work_shift"] = {
        "duration_seconds": terms.duration_seconds,
        "resource_id": terms.resource_id,
        "wage": format(terms.wage, "f"),
        "can_work_shift": _can_complete_paid_shift(
            posting, terms, actor_id=actor_id, actor=actor, entities=entities,
            resources=resources, current_tick=current_tick,
        ),
    }
    return projected


def _can_complete_paid_shift(
    posting: dict[str, Any],
    terms: WorkShiftTerms,
    *,
    actor_id: str,
    actor: dict[str, Any],
    entities: dict[str, Any],
    resources: dict[str, Any],
    current_tick: int,
) -> bool:
    employer_id = posting.get("employer_id")
    location_id = posting.get("location_id")
    employer = entities.get(employer_id) if isinstance(employer_id, str) else None
    workplace = entities.get(location_id) if isinstance(location_id, str) else None
    if (
        actor.get("kind") not in {"character", "person"}
        or actor.get("status", "active") != "active"
        or actor.get("location_id") != location_id
        or employer is None
        or employer.get("kind") not in {"organization", "polity", "faction", "company", "government"}
        or employer.get("status", "active") != "active"
        or workplace is None
        or workplace.get("kind") not in {"place", "location"}
        or workplace.get("status", "active") != "active"
        or posting.get("status") != "open"
        or current_tick < posting.get("valid_from_tick", 0)
    ):
        return False
    valid_until = posting.get("valid_until_tick")
    if current_tick > 2**63 - 1 - terms.duration_seconds:
        return False
    if valid_until is not None and current_tick + terms.duration_seconds > valid_until:
        return False

    worker_holders = [
        (account_id, account)
        for account_id, account in resources.items()
        if isinstance(account, dict)
        and account.get("owner_id") == actor_id
        and account.get("account_type", "holder") == "holder"
        and account.get("resource_id") == terms.resource_id
        and isinstance(account_id, str)
    ]
    if not worker_holders:
        return False

    treasury = employer.get("components", {}).get("treasury", {})
    account_map = treasury.get("accounts", {}) if isinstance(treasury, dict) else {}
    employer_account_id = account_map.get(terms.resource_id) if isinstance(account_map, dict) else None
    employer_account = resources.get(employer_account_id) if isinstance(employer_account_id, str) else None
    if not isinstance(employer_account, dict):
        return False
    if (
        employer_account.get("account_type", "holder") != "holder"
        or employer_account.get("owner_id") != employer_id
        or employer_account.get("resource_id") != terms.resource_id
    ):
        return False
    try:
        balance = Decimal(str(employer_account.get("balance")))
    except Exception:
        return False
    return balance.is_finite() and balance >= terms.wage


def _local_market_offers(
    entities: dict[str, Any],
    actor_id: str,
    location_id: str | None,
    observed_entities: set[str],
    tick: int,
) -> dict[str, Any]:
    empty = {"currency_resource_id": None, "offers": []}
    if not location_id:
        return empty
    market_entity = entities.get(location_id)
    if not isinstance(market_entity, dict):
        return empty
    if market_entity.get("kind") not in {"place", "location"}:
        return empty
    if market_entity.get("status", "active") != "active":
        return empty
    market_components = market_entity.get("components", {})
    if not isinstance(market_components, dict):
        return empty
    if market_components.get("hidden", False) and location_id not in observed_entities:
        return empty
    try:
        market = MarketComponent.model_validate(market_components.get("market"))
    except Exception:
        return empty

    posted = []
    for offer in market.offers:
        if not offer.active:
            continue
        seller_id = str(offer.seller_id)
        seller = entities.get(seller_id)
        if not isinstance(seller, dict) or seller.get("status", "active") != "active":
            continue
        if seller.get("location_id") != location_id:
            continue
        if seller_id != actor_id and seller.get("components", {}).get("hidden", False):
            if seller_id not in observed_entities:
                continue
        posted.append({
            "listing_id": str(offer.listing_id),
            "resource_id": offer.resource_id,
            "unit_price_minor": str(offer.unit_price_minor),
            "minimum_quantity": _canonical_public_decimal(offer.minimum_quantity),
        })
    posted.sort(key=lambda row: row["listing_id"])
    return {"currency_resource_id": market.currency_resource_id, "offers": posted}


def _canonical_public_decimal(value: Decimal) -> str:
    rendered = format(value, "f")
    if "." in rendered:
        rendered = rendered.rstrip("0").rstrip(".")
    return rendered or "0"


def _participant_promises(state: dict[str, Any], actor_id: str) -> list[dict[str, Any]]:
    trust_receipts = _validated_promise_trust_receipts(state)
    promises = []
    for promise in state.get("relationship_promises", []):
        promisor, promisee = promise["promisor_id"], promise["promisee_id"]
        if actor_id not in (promisor, promisee):
            continue
        role = "promisor" if actor_id == promisor else "promisee"
        terms = promise["terms"]
        projected = {
            "promise_id": promise["id"],
            "role": role,
            "counterparty_id": promisee if role == "promisor" else promisor,
            "text": promise["private_text"],
            "status": promise["status"],
            "created_tick": promise["created_tick"],
            "deadline_tick": promise["deadline_tick"],
            "resolved_tick": promise.get("resolved_tick"),
            "terms": {key: deepcopy(terms[key]) for key in ("kind", "resource_id", "quantity")},
        }
        if promise.get("status") == "breached" and promise.get("trust_policy_id") == PROMISE_TRUST_POLICY_ID:
            promise_id = _uuid_or_none(promise.get("id"))
            outcome_event_id = _uuid_or_none(promise.get("outcome_event_id"))
            receipt = trust_receipts.get(promise_id) if promise_id is not None else None
            if (
                receipt is not None
                and receipt.promise_id == promise_id
                and receipt.policy_id == PROMISE_TRUST_POLICY_ID
                and receipt.outcome == "breached"
                and outcome_event_id is not None
                and receipt.outcome_event_id == outcome_event_id
            ):
                late_returned = receipt.late_return_event_id is not None
                projected["late_returned"] = late_returned
                projected["can_return_late"] = role == "promisor" and not late_returned
        promises.append(projected)
    return promises


def _validated_promise_trust_receipts(state: dict[str, Any]):
    raw_projection = state.get("promise_trust")
    if not isinstance(raw_projection, dict):
        return {}
    try:
        projection = PromiseTrustProjection.from_payload(raw_projection)
    except InvalidEvent:
        return {}
    return projection.receipts


def _uuid_or_none(value: Any) -> UUID | None:
    try:
        return UUID(str(value))
    except (TypeError, ValueError, AttributeError):
        return None


def _participant_shipments(
    entities: dict[str, Any], actor_id: str, actor: dict[str, Any],
) -> list[dict[str, Any]]:
    shipments = []
    for entity_id, entity in entities.items():
        if entity.get("kind") != "shipment":
            continue
        shipment = entity.get("components", {}).get("shipment")
        if not isinstance(shipment, dict) or actor_id not in (
            shipment.get("sender_id"), shipment.get("destination_owner_id"),
        ):
            continue
        receiver = shipment.get("destination_owner_id") == actor_id
        shipments.append({
            "shipment_id": entity_id,
            "role": "receiver" if receiver else "sender",
            "status": shipment["status"],
            "departure_tick": shipment["departure_tick"],
            "arrival_tick": shipment["arrival_tick"],
            "arrived_tick": shipment.get("arrived_tick"),
            "origin_place_id": shipment["origin_place_id"],
            "destination_place_id": shipment["destination_place_id"],
            "origin_place_name": entities.get(shipment["origin_place_id"], {}).get("name"),
            "destination_place_name": entities.get(shipment["destination_place_id"], {}).get("name"),
            "receiving_custody": ("place_depot" if shipment.get("destination_owner_id") == shipment["destination_place_id"]
                                  else "designated_holder"),
            "cargo": _shipment_cargo_summary(shipment),
            "can_receive": receiver and shipment["status"] == "awaiting_receipt"
            and actor.get("status", "active") == "active" and actor.get("location_id") == shipment["destination_place_id"],
        })
    return shipments


def _shipment_cargo_summary(shipment: dict[str, Any]) -> list[dict[str, Any]]:
    spoiled_by_lot = {lot["transit_lot_id"]: Decimal(str(lot["quantity"])) for lot in shipment.get("spoiled_lots", [])}
    delivered_by_lot = {lot["transit_lot_id"]: Decimal(str(lot["quantity"])) for lot in shipment.get("delivered_lots", [])}
    status = shipment["status"]
    legacy_delivery = status == "delivered" and shipment.get("lifecycle_version", "legacy_v1") == "legacy_v1"
    result = []
    with localcontext() as context:
        context.prec = 80
        for line in shipment["cargo"]:
            summary = {"resource_id": line["resource_id"], "quantity": deepcopy(line["quantity"])}
            if spoiled_by_lot:
                spoiled = sum((spoiled_by_lot.get(lot["transit_lot_id"], Decimal(0)) for lot in line["lots"]), Decimal(0))
                if spoiled:
                    summary["spoiled_quantity"] = format(spoiled, "f")
                    summary["remaining_quantity"] = format(Decimal(str(line["quantity"])) - spoiled, "f")
            if legacy_delivery:
                summary["delivered_quantity"] = format(Decimal(str(line["quantity"])), "f")
            elif delivered_by_lot or status in {"delivered", "spoiled"}:
                delivered = sum((delivered_by_lot.get(lot["transit_lot_id"], Decimal(0)) for lot in line["lots"]), Decimal(0))
                summary["delivered_quantity"] = format(delivered, "f")
            result.append(summary)
    return result


def _case_anchors(
    entities: dict[str, Any], actor: dict[str, Any], observed_entities: set[str],
) -> list[dict[str, Any]]:
    location = actor.get("location_id")
    if not location or actor.get("status", "active") != "active":
        return []
    anchors = []
    for entity_id, entity in sorted(entities.items()):
        if entity.get("kind") != "item" or entity.get("status", "active") != "active":
            continue
        components = entity.get("components", {})
        case = components.get("investigation_case")
        if not isinstance(case, dict) or not {"case_id", "title", "brief"} <= case.keys():
            continue
        if components.get("hidden", False) and entity_id not in observed_entities:
            continue
        place = components.get("place")
        place_parent = place.get("parent_id") if isinstance(place, dict) else None
        if location not in (entity.get("location_id"), components.get("parent_id") or place_parent):
            continue
        anchors.append({"anchor_id": entity_id, **{key: deepcopy(case[key]) for key in ("case_id", "title", "brief")}})
    return anchors


def _combat_truce_for_actor(raw: object, actor_id: str) -> dict[str, Any] | None:
    try:
        offer = CombatTruceOffer.model_validate(raw)
    except (TypeError, ValueError):
        return None
    if actor_id not in {str(offer.proposer_id), str(offer.recipient_id)}:
        return None
    return offer.model_dump(mode="json", include={
        "offer_id", "proposer_id", "recipient_id", "status", "offered_tick", "responded_tick",
    })


def _invitation_for_actor(
    raw: object, actor_id: str, *, entities: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    try:
        invitation = RelationshipInvitation.model_validate(raw)
    except ValueError:
        return None
    if actor_id not in {str(invitation.proposer_id), str(invitation.invitee_id)}:
        return None
    projected = invitation.model_dump(mode="json")
    if entities is not None and invitation.meeting is not None and invitation.meeting.venue_id is not None:
        venue = entities.get(str(invitation.meeting.venue_id), {})
        if venue.get("name"):
            projected["meeting_venue_name"] = venue["name"]
    return projected


def _meeting_reflection_for_actor(raw: object, actor_id: str) -> dict[str, Any] | None:
    try:
        reflection = MeetingReflection.model_validate(raw)
    except ValueError:
        return None
    if str(reflection.actor_id) != actor_id:
        return None
    return reflection.model_dump(mode="json")


def _partnership_for_actor(
    raw: object,
    actor_id: str,
    *,
    entities: dict[str, Any] | None = None,
    current_tick: int | None = None,
    calendar: object = None,
    current_player_id: object = None,
    include_actions: bool = False,
) -> dict[str, Any] | None:
    try:
        partnership = RelationshipPartnership.model_validate(raw)
    except (TypeError, ValueError):
        return None
    if actor_id not in {str(partnership.proposer_id), str(partnership.partner_id)}:
        return None
    author_id = str(partnership.current_proposal_author_id)
    recipient_id = str(partnership.current_proposal_recipient_id)
    view = partnership.model_dump(mode="json", include={
        "partnership_id", "proposer_id", "partner_id", "status", "proposed_tick", "updated_tick",
        "revision", "proposal_terms",
    })
    view.update({
        "current_proposal_author_id": author_id,
        "current_proposal_recipient_id": recipient_id,
    })
    if include_actions:
        entity_map = entities or {}
        actor = entity_map.get(actor_id)
        active_actor = isinstance(actor, dict) and actor.get("status", "active") == "active"
        can_update = _partnership_pair_can_update(partnership, entity_map, current_tick, calendar)
        pending = partnership.status == "pending"
        view.update({
            "can_revise": pending and can_update and actor_id == author_id,
            "can_counter": pending and can_update and actor_id == recipient_id,
            "can_accept": pending and can_update and actor_id == recipient_id,
            "can_decline": pending and active_actor and actor_id == recipient_id,
            "can_withdraw": pending and active_actor and actor_id == author_id,
            "can_request_response": (
                pending and actor_id == author_id and current_player_id is not None
                and recipient_id != str(current_player_id)
            ),
        })
    return view


def _partnership_pair_can_update(
    partnership: RelationshipPartnership,
    entities: dict[str, Any],
    current_tick: int | None,
    calendar: object,
) -> bool:
    if current_tick is None:
        return False
    first = entities.get(str(partnership.proposer_id))
    second = entities.get(str(partnership.partner_id))
    if not isinstance(first, dict) or not isinstance(second, dict):
        return False
    first_location = first.get("location_id")
    second_location = second.get("location_id")
    if (
        first.get("kind") not in {"character", "person"}
        or second.get("kind") not in {"character", "person"}
        or first.get("status", "active") != "active"
        or second.get("status", "active") != "active"
        or not isinstance(first_location, str)
        or first_location != second_location
    ):
        return False
    place = entities.get(first_location)
    if (
        not isinstance(place, dict)
        or place.get("kind") not in {"place", "location"}
        or place.get("status", "active") != "active"
    ):
        return False
    try:
        first_life = LifeComponent.model_validate(first.get("components", {}).get("life"))
        second_life = LifeComponent.model_validate(second.get("components", {}).get("life"))
        calendar_config = CalendarConfig.model_validate(calendar or {})
    except (AttributeError, TypeError, ValueError):
        return False
    year_ticks = calendar_config.day_seconds * calendar_config.year_days
    return all(
        life.life_stage in {"adult", "elder"}
        and (current_tick - life.birth_tick) // year_ticks >= 18
        for life in (first_life, second_life)
    )


def project_relationship_disclosure_record(
    raw: object,
    recipient_id: str,
    *,
    current_tick: int,
    discloser_id: str | None = None,
    entities: dict[Any, Any] | None = None,
) -> dict[str, Any] | None:
    """Return only the safe, recipient-held assertion and its bounded provenance."""
    try:
        record = KnowledgeRecord.model_validate(raw)
    except (TypeError, ValueError):
        return None
    receipt = record.relationship_disclosure
    if (
        receipt is None
        or record.holder_id is None
        or str(record.holder_id) != recipient_id
        or str(receipt.recipient_id) != recipient_id
        or record.subject_id != receipt.discloser_id
        or record.received_tick > current_tick
        or receipt.disclosed_tick > current_tick
        or (record.valid_from_tick is not None and record.valid_from_tick > current_tick)
        or (discloser_id is not None and str(receipt.discloser_id) != discloser_id)
    ):
        return None
    entity_map = entities or {}

    def safe_name(entity_id: UUID) -> str:
        entity = entity_map.get(str(entity_id), entity_map.get(entity_id))
        if isinstance(entity, str):
            name = entity
        else:
            name = entity.get("name") if isinstance(entity, dict) else getattr(entity, "name", None)
        return name if isinstance(name, str) and name.strip() else "一名人物"

    opposite_name = safe_name(receipt.opposite_participant_id)
    discloser_name = safe_name(receipt.discloser_id)
    return {
        "record_id": str(record.id),
        "statement": f"告知者当时表示自己与{opposite_name}已成为伴侣。"[:600],
        "source": "past_statement_by_discloser",
        "discloser_id": str(receipt.discloser_id),
        "discloser_name": discloser_name,
        "opposite_participant_id": str(receipt.opposite_participant_id),
        "opposite_participant_name": opposite_name,
        "location_id": str(receipt.location_id),
        "disclosed_tick": receipt.disclosed_tick,
    }


def _own_relationship_disclosures(
    state: dict[str, Any], actor_id: str, current_tick: int, *, limit: int = 32,
) -> list[dict[str, Any]]:
    disclosures = [
        safe for raw in state.get("knowledge", [])
        if (safe := project_relationship_disclosure_record(
            raw, actor_id, current_tick=current_tick, entities=state.get("entities"),
        )) is not None
    ]
    disclosures.sort(key=lambda row: (-row["disclosed_tick"], row["record_id"]))
    return disclosures[:limit]


def project_sent_relationship_disclosures(
    state: dict[str, Any], discloser_id: str, *, current_tick: int, limit: int = 32,
) -> list[dict[str, Any]]:
    """Project a bounded summary of only the actor's own recorded disclosures."""
    entities = state.get("entities", {})
    disclosures = []
    for raw in state.get("knowledge", []):
        if isinstance(raw, dict) and not raw.get("relationship_disclosure"):
            continue
        try:
            record = KnowledgeRecord.model_validate(raw)
        except (TypeError, ValueError):
            continue
        receipt = record.relationship_disclosure
        if receipt is None or str(receipt.discloser_id) != discloser_id:
            continue
        # Reuse the recipient projection's checks and safe assertion wording.
        received = project_relationship_disclosure_record(
            record,
            str(receipt.recipient_id),
            current_tick=current_tick,
            discloser_id=discloser_id,
            entities=entities,
        )
        if received is None:
            continue

        recipient = entities.get(str(receipt.recipient_id), entities.get(receipt.recipient_id))
        recipient_name = recipient.get("name") if isinstance(recipient, dict) else getattr(recipient, "name", None)
        if not isinstance(recipient_name, str) or not recipient_name.strip():
            recipient_name = "一名人物"
        disclosures.append({
            "record_id": received["record_id"],
            "statement": received["statement"],
            "source": received["source"],
            "discloser_id": received["discloser_id"],
            "discloser_name": received["discloser_name"],
            "recipient_id": str(receipt.recipient_id),
            "recipient_name": recipient_name,
            "opposite_participant_id": received["opposite_participant_id"],
            "opposite_participant_name": received["opposite_participant_name"],
            "disclosed_tick": received["disclosed_tick"],
        })
    disclosures.sort(key=lambda row: (-row["disclosed_tick"], row["record_id"]))
    return disclosures[:min(32, max(0, limit))]


def _relationship_disclosure_options(
    state: dict[str, Any], actor_id: str, visible_entities: dict[str, Any], current_tick: int,
) -> list[dict[str, Any]]:
    """List active own partnerships and visible same-scene third-party recipients."""
    entities = state.get("entities", {})
    actor = entities.get(actor_id)
    if not isinstance(actor, dict) or actor.get("status", "active") != "active":
        return []
    location_id = actor.get("location_id")
    location = entities.get(location_id, {}) if isinstance(location_id, str) else {}
    if not isinstance(location, dict) or location.get("kind") not in {"place", "location"}:
        return []

    recipients = []
    for ident, entity in visible_entities.items():
        raw = entities.get(ident)
        if (
            ident == actor_id
            or not isinstance(raw, dict)
            or raw.get("kind") not in {"character", "person"}
            or raw.get("status", "active") != "active"
            or raw.get("location_id") != location_id
        ):
            continue
        recipients.append({"recipient_id": ident, "recipient_name": raw.get("name")})
    recipients.sort(key=lambda row: ((row["recipient_name"] or "").casefold(), row["recipient_id"]))
    if not recipients:
        return []

    existing_disclosures: set[tuple[str, int, str, str]] = set()
    for raw in state.get("knowledge", []):
        try:
            record = KnowledgeRecord.model_validate(raw)
        except (TypeError, ValueError):
            continue
        receipt = record.relationship_disclosure
        if receipt is not None:
            existing_disclosures.add((
                str(receipt.source_partnership_id), receipt.source_revision,
                str(receipt.discloser_id), str(receipt.recipient_id),
            ))

    options = []
    partnerships = []
    for raw in state.get("relationship_partnerships", []):
        try:
            partnership = RelationshipPartnership.model_validate(raw)
        except (TypeError, ValueError):
            continue
        if (
            partnership.status != "active"
            or partnership.updated_tick > current_tick
            or actor_id not in {str(partnership.proposer_id), str(partnership.partner_id)}
        ):
            continue
        partnerships.append(partnership)
    partnerships.sort(key=lambda item: (item.updated_tick, str(item.partnership_id)))

    for partnership in partnerships:
        opposite_id = (
            partnership.partner_id if actor_id == str(partnership.proposer_id) else partnership.proposer_id
        )
        opposite = entities.get(str(opposite_id), {})
        if not isinstance(opposite, dict):
            continue
        recipient_options = [
            row for row in recipients
            if row["recipient_id"] != str(opposite_id)
            and (
                str(partnership.partnership_id), partnership.revision, actor_id, row["recipient_id"]
            ) not in existing_disclosures
        ]
        if not recipient_options:
            continue
        options.append({
            "partnership_id": str(partnership.partnership_id),
            "expected_revision": partnership.revision,
            "opposite_participant_id": str(opposite_id),
            "opposite_participant_name": opposite.get("name"),
            "recipient_options": recipient_options,
        })
    return options


def _travel_for_actor(raw: object, actor_id: str) -> dict[str, Any] | None:
    try:
        journey = TravelJourney.model_validate(raw)
    except ValueError:
        return None
    if str(journey.actor_id) != actor_id:
        return None
    return journey.model_dump(mode="json", exclude={"hazard_tick"})


def project_event(
    event: dict[str, Any], actor_id: str, *, entities: dict[Any, Any] | None = None,
) -> dict[str, Any] | None:
    """Use a precomputed audience-safe payload; do not expose raw event payloads."""
    if event.get("type") == WAYPOINT_OPPORTUNITY_DECISION_RECORDED_EVENT_TYPE:
        payload = event.get("payload")
        if not isinstance(payload, dict) or event.get("visibility") != {"policy": "subject_ids", "subject_ids": [actor_id]}:
            return None
        try:
            decision = WaypointOpportunityDecisionRecordedEvent.model_validate(payload | {
                "event_id": event.get("event_id"), "visibility": event.get("visibility"),
            })
        except (TypeError, ValueError):
            return None
        return {key: deepcopy(event[key]) for key in ("event_id", "type", "tick", "sequence") if key in event} | {
            "payload": {"choice": decision.record.choice, "journey_id": str(decision.record.journey_id)},
        }
    if event.get("type") == "core.cultivation_breakthrough_resolved.v1":
        payload = event.get("payload")
        if not isinstance(payload, dict) or event.get("visibility") != {"policy": "subject_ids", "subject_ids": [actor_id]}:
            return None
        try:
            breakthrough = CultivationBreakthroughResolvedEvent.model_validate(payload | {
                "event_id": event.get("event_id"), "visibility": event.get("visibility"),
            })
        except (TypeError, ValueError):
            return None
        if str(breakthrough.plan.actor_id) != actor_id:
            return None
        return {key: deepcopy(event[key]) for key in ("event_id", "type", "tick", "sequence") if key in event} | {
            "payload": breakthrough.plan.model_dump(mode="json", include={
                "outcome", "source_realm", "target_realm", "qi_cost", "duration_ticks", "failure_damage",
            }),
        }
    if event.get("type") == "core.combat_truce_changed.v1":
        payload = event.get("payload")
        if not isinstance(payload, dict):
            return None
        try:
            change = CombatTruceChangedEvent.model_validate(payload | {
                "event_id": event.get("event_id"), "visibility": event.get("visibility"),
            })
        except (TypeError, ValueError):
            return None
        truce = _combat_truce_for_actor(change.resulting_offer, actor_id)
        if truce is None:
            return None
        return {key: deepcopy(event[key]) for key in ("event_id", "type", "tick", "sequence") if key in event} | {
            "payload": {"operation": change.operation, "truce": truce},
        }
    if event.get("type") == "core.relationship_disclosure_recorded.v1":
        payload = event.get("payload")
        if not isinstance(payload, dict):
            return None
        try:
            disclosure_event = parse_relationship_disclosure_event(payload | {
                "type": event.get("type"),
                "event_id": event.get("event_id"),
                "visibility": event.get("visibility"),
            })
        except (TypeError, ValueError):
            return None
        safe_receipt = project_relationship_disclosure_record(
            disclosure_event.record,
            actor_id,
            current_tick=disclosure_event.record.received_tick,
            entities=entities,
        )
        if safe_receipt is None:
            return None
        return {
            key: deepcopy(event[key])
            for key in ("event_id", "type", "tick", "sequence")
            if key in event
        } | {"payload": safe_receipt}
    if event.get("type") == "core.relationship_invitation_changed.v1":
        payload = event.get("payload")
        original = payload.get("resulting_invitation") if isinstance(payload, dict) else None
        invitation = _invitation_for_actor(original, actor_id)
        if invitation is None:
            return None
        audience = sorted([invitation["proposer_id"], invitation["invitee_id"]])
        if event.get("visibility") != {"policy": "subject_ids", "subject_ids": audience}:
            return None
        return {key: deepcopy(event[key]) for key in ("event_id", "type", "tick", "sequence") if key in event} | {
            "payload": {"invitation": invitation},
        }
    if event.get("type") == "core.meeting_reflection_recorded.v1":
        if event.get("visibility") != {"policy": "subject_ids", "subject_ids": [actor_id]}:
            return None
        payload = event.get("payload")
        reflection = _meeting_reflection_for_actor(payload.get("reflection"), actor_id) if isinstance(payload, dict) else None
        if reflection is None:
            return None
        return {key: deepcopy(event[key]) for key in ("event_id", "type", "tick", "sequence") if key in event} | {
            "payload": {"reflection": reflection},
        }
    if event.get("type") == "core.relationship_partnership_changed.v1":
        payload = event.get("payload")
        partnership = _partnership_for_actor(payload.get("resulting_partnership"), actor_id) if isinstance(payload, dict) else None
        if partnership is None:
            return None
        audience = sorted((partnership["proposer_id"], partnership["partner_id"]))
        if event.get("visibility") != {"policy": "subject_ids", "subject_ids": audience}:
            return None
        return {key: deepcopy(event[key]) for key in ("event_id", "type", "tick", "sequence") if key in event} | {
            "payload": {"partnership": partnership},
        }
    if event.get("type") == "core.shipment_relief_distributed.v1":
        payload = event.get("payload")
        if not isinstance(payload, dict) or not _visible(event, actor_id):
            return None
        try:
            delivery = ShipmentReliefDistributedEvent.model_validate(payload | {
                "event_id": event.get("event_id"), "visibility": event.get("visibility"),
            })
        except ValueError:
            return None
        plan = delivery.plan
        return {key: deepcopy(event[key]) for key in ("event_id", "type", "tick", "sequence") if key in event} | {
            "payload": {
                "shipment_id": str(plan.shipment_id), "recipient_id": str(plan.recipient_id),
                "depot_id": str(plan.depot_id), "resource_id": plan.resource_id, "quantity": str(plan.quantity),
                "authorized_by_actor_id": str(plan.actor_id),
                "authorizing_organization_id": str(plan.organization_id),
            }
        }
    if event.get("type") == "core.travel_journey_changed.v1":
        payload = event.get("payload")
        if not isinstance(payload, dict) or event.get("visibility") != {"policy": "subject_ids", "subject_ids": [actor_id]}:
            return None
        try:
            transition = TravelJourneyChangedEvent.model_validate(payload | {
                "event_id": event.get("event_id"), "visibility": event.get("visibility"),
            })
        except ValueError:
            return None
        journey = _travel_for_actor(transition.journey, actor_id)
        if journey is None:
            return None
        return {key: deepcopy(event[key]) for key in ("event_id", "type", "tick", "sequence") if key in event} | {
            "payload": {"journey": journey},
        }
    if event.get("type") == "core.private_gift_changed.v1":
        payload = event.get("payload")
        if not isinstance(payload, dict):
            return None
        try:
            change = PrivateGiftChangedEvent.model_validate(payload | {
                "event_id": event.get("event_id"), "visibility": event.get("visibility"),
            })
        except (TypeError, ValueError):
            return None
        offer = change.offer
        participants = {str(offer.giver_id), str(offer.recipient_id)}
        if actor_id not in participants or change.visibility != {
            "policy": "subject_ids", "subject_ids": sorted(participants),
        }:
            return None
        return {key: deepcopy(event[key]) for key in ("event_id", "type", "tick", "sequence") if key in event} | {
            "payload": {"operation": change.operation, "offer": _private_gift_offer_view(offer)},
        }
    promise_result = _project_promise_trust_result(event, actor_id)
    if promise_result is not None:
        return promise_result
    if not _visible(event, actor_id):
        return None
    views = event.get("audience_views", {})
    payload = views.get(actor_id, views.get("public"))
    if payload is None:
        # Visible existence alone does not authorize every field in an event.
        return None
    return {
        key: deepcopy(event[key])
        for key in ("event_id", "type", "tick", "sequence")
        if key in event
    } | {"payload": deepcopy(payload)}


_PROMISE_TRUST_RESULTS = {
    "core.promise_item_returned.v2": (
        "core.promise_item_returned.v1", "transfer_tick", "fulfilled", "source_owner_id", "recipient_owner_id",
    ),
    "core.promise_breached.v2": (
        "core.promise_breached.v1", "resolved_tick", "breached", "promisor_id", "promisee_id",
    ),
    "core.promise_item_returned_late.v1": (
        "core.promise_item_returned.v1", "transfer_tick", "late_returned", "source_owner_id", "recipient_owner_id",
    ),
}


def _project_promise_trust_result(event: dict[str, Any], actor_id: str) -> dict[str, Any] | None:
    """Expose only the settlement result to the two participants of a v2 promise event."""
    event_type = event.get("type")
    contract = _PROMISE_TRUST_RESULTS.get(event_type) if isinstance(event_type, str) else None
    if contract is None or event.get("visibility") != {"policy": "author_only"}:
        return None

    expected_settlement_type, time_field, status, first_participant_field, second_participant_field = contract
    wrapper_payload = event.get("payload", event)
    if not isinstance(wrapper_payload, dict):
        return None
    settlement = wrapper_payload.get("settlement")
    if not isinstance(settlement, dict) or settlement.get("type") != expected_settlement_type:
        return None
    promise_id = settlement.get("promise_id")
    if not isinstance(promise_id, str) or not promise_id.strip() or "tick" not in event:
        return None

    visibility = settlement.get("visibility")
    if not isinstance(visibility, dict) or set(visibility) != {"policy", "subject_ids"}:
        return None
    if visibility.get("policy") != "subject_ids":
        return None
    subject_ids = visibility.get("subject_ids")
    if (
        not isinstance(subject_ids, list)
        or len(subject_ids) != 2
        or any(not isinstance(subject_id, str) or not subject_id.strip() for subject_id in subject_ids)
        or len(set(subject_ids)) != 2
        or subject_ids != sorted(subject_ids)
        or actor_id not in subject_ids
    ):
        return None

    # The nested settlement time is checked for shape only; the committed event
    # envelope supplies the displayed tick and remains the source of event time.
    settlement_tick = settlement.get(time_field)
    if type(settlement_tick) is not int:
        return None
    participants = (settlement.get(first_participant_field), settlement.get(second_participant_field))
    if (
        any(not isinstance(participant_id, str) or not participant_id.strip() for participant_id in participants)
        or participants[0] == participants[1]
        or subject_ids != sorted(participants)
    ):
        return None
    return {
        **{
            key: deepcopy(event[key])
            for key in ("event_id", "type", "tick", "sequence")
            if key in event
        },
        "payload": {"promise_id": promise_id, "status": status},
    }
