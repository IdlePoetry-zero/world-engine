"""A modern work, appointment, and purchase chain using deterministic model proposals."""

from decimal import Decimal
from uuid import UUID, uuid4

from test_modern_multi_invitation_scenario import _person
from test_work_chat_contract import (
    ACTOR_ID, DAY, EMPLOYER_ACCOUNT_ID, EMPLOYER_ID, ITEM_RESOURCE_ID, LISTING_ID,
    SELLER_ID, STOCK_ACCOUNT_ID, WORKER_ACCOUNT_ID, WORKPLACE_ID,
    _with_work_routine, _work_routine_api, _world_with_market,
)

from world_engine.adapters.models import RoleTask
from world_engine.api.contracts import MessageRequest
from world_engine.application.simulation_jobs import SimulationJobs
from world_engine.domain.models import CommandEnvelope, JobStatus, ResourceAccount, WorldState
from world_engine.rules.contracts import RuleContext
from world_engine.rules.engine import RuleEngine
from world_engine.simulation.contracts import DecisionResponse


def test_modern_work_skips_the_shift_chosen_for_an_appointment_and_purchase_is_separate(tmp_path):
    first_shift, shift_length = 12_600, 3_600
    meeting_start = first_shift + DAY
    meeting_end = meeting_start + shift_length
    next_shift = first_shift + 2 * DAY
    authorized_until = 30 * DAY
    currency = "modern.credit"
    coworker_id, seller_money_id, buyer_goods_id = (uuid4() for _ in range(3))
    state, assignment = _world_with_market(currency_resource_id=currency)
    raw = _with_work_routine(state).model_dump(mode="json")
    player = raw["entities"][str(ACTOR_ID)]
    player["name"] = "Morgan"
    player["components"].update(_person(ACTOR_ID, "Morgan", WORKPLACE_ID, "punctual").components)
    raw["entities"][str(coworker_id)] = _person(coworker_id, "Rae", WORKPLACE_ID, "patient").model_dump(mode="json")
    raw["entities"][str(WORKPLACE_ID)]["name"] = "Station Concourse"
    raw["entities"][str(WORKPLACE_ID)]["components"]["market"]["offers"][0]["unit_price_minor"] = 15
    raw["metadata"]["work_shift_terms"][0]["resource_id"] = currency
    raw["entities"][str(EMPLOYER_ID)]["components"]["treasury"]["accounts"] = {currency: str(EMPLOYER_ACCOUNT_ID)}
    for account_id in (WORKER_ACCOUNT_ID, EMPLOYER_ACCOUNT_ID):
        raw["resources"][str(account_id)]["resource_id"] = currency
    raw["resources"][str(seller_money_id)] = ResourceAccount(
        resource_id=currency, balance="0", owner_id=SELLER_ID,
    ).model_dump(mode="json")
    raw["resources"][str(buyer_goods_id)] = ResourceAccount(
        resource_id=ITEM_RESOURCE_ID, balance="0", owner_id=ACTOR_ID,
    ).model_dump(mode="json")
    state = WorldState.model_validate(raw)
    client, database, repository, scope, world, models = _work_routine_api(tmp_path, state, [])
    world_id, branch_id = world.world.world_id, world.branch.branch_id
    world_path = f"/api/v1/worlds/{world_id}"
    jobs = SimulationJobs(repository)

    def head():
        return repository.read_world(scope, world_id, branch_id)

    def submit(path, payload, key):
        response = client.post(path, json=payload, headers={"Idempotency-Key": key})
        assert response.is_success, response.json()
        return response.json()

    def action(text, proposed):
        models.outputs.append({"action": proposed, "clarification": ""})
        request_id = uuid4()
        request = MessageRequest(
            request_id=request_id, world_id=world_id, branch_id=branch_id, session_id=branch_id, actor_id=ACTOR_ID,
            expected_state_version=str(head().branch.state_version), mode="action", text=text,
        )
        reply = submit(f"{world_path}/messages", request.model_dump(mode="json"), str(request_id))
        assert reply["kind"] == "action" and reply["world_effect"] is True, reply
        return reply

    def run(job_id, checkpoint, *, job_type="simulation.long_action"):
        lease = repository.claim_job("modern-play-test", job_type=job_type)
        assert lease is not None and lease.job_id == job_id
        for _ in range(120):
            result = jobs.advance(scope, lease)
            if getattr(result, "decision_requests", ()):
                decision, = result.decision_requests
                assert (decision.kind, decision.actor_id, decision.at_tick) == (
                    "npc.meeting_attendance", coworker_id, meeting_start,
                )
                jobs.advance(scope, lease, model_decision=DecisionResponse(
                    request_id=decision.request_id, state_version=decision.state_version,
                    at_tick=decision.at_tick, option_id="attend",
                ))
            job = repository.read_job(scope, job_id)
            if checkpoint(job):
                return job
            assert job.status is JobStatus.RUNNING, job.result
        raise AssertionError(f"The {job_type} job did not reach its checkpoint by tick {job.committed_through_tick}")

    def advance_to(tick, key):
        current = head()
        queued = submit(f"{world_path}/world-advance", {
            "branch_id": str(branch_id),
            "expected_state_version": str(current.branch.state_version),
            "duration_seconds": tick - current.state.current_tick,
        }, key)
        run(UUID(queued["job_id"]), lambda job: job.status is JobStatus.SUCCEEDED, job_type="simulation.world_advance")
        assert head().state.current_tick == tick

    with client:
        try:
            command_id = uuid4()
            proposal = {
                "kind": "propose_relationship_invitation", "target_id": str(ACTOR_ID),
                "meeting": {"start_tick": meeting_start, "end_tick": meeting_end},
            }
            before = head()
            proposed = RuleEngine().evaluate(before.state, proposal, RuleContext(
                world_id=world_id, branch_id=branch_id, command_id=command_id,
                actor_id=coworker_id, ruleset_version=before.world.ruleset_version,
            ))
            assert proposed.outcome.status == "succeeded", proposed.outcome
            event, = proposed.events
            repository.commit_command(scope, world_id, branch_id, CommandEnvelope(
                command_id=command_id, actor_id=coworker_id, idempotency_key="coworker-proposes-appointment",
                expected_state_version=before.branch.state_version,
                command_type="action.propose_relationship_invitation", payload=proposal,
            ), proposed.events)
            invitation_id = str(event.resulting_invitation.invitation_id)
            action("接受 Rae 提出的见面邀约。", {
                "kind": "resolve_relationship_invitation", "invitation_id": invitation_id, "operation": "accept",
            })

            queued = submit(f"{world_path}/long-actions", {
                "branch_id": str(branch_id),
                "expected_state_version": str(head().branch.state_version),
                "duration_seconds": authorized_until, "activity": "work",
                "work_schedule": {
                    "assignment_id": str(assignment.assignment_id), "first_shift_tick": first_shift,
                    "repeat_every_days": 1,
                    "purchase_target": {"listing_id": str(LISTING_ID), "quantity": "2"},
                },
            }, "month-of-work-for-purchase")
            work_id = UUID(queued["job_id"])
            job_path = f"/api/v1/jobs/{work_id}"
            job = run(work_id, lambda job: job.payload.get("work_progress", {}).get("shifts_paid") == 1)
            authorized_plan = job.payload["plan"]
            assert (authorized_plan["started_tick"], authorized_plan["target_tick"]) == (0, authorized_until)
            assert job.committed_through_tick == first_shift + shift_length
            assert head().state.resources[WORKER_ACCOUNT_ID].balance == Decimal("17.50")
            assert head().state.resources[EMPLOYER_ACCOUNT_ID].balance == Decimal("87.50")

            paused = submit(f"{job_path}/control", {"action": "pause"}, "choose-appointment")
            assert paused["status"] == "paused"
            advance_to(meeting_start, "wait-until-appointment")
            action("暂停工作，按约赴会。", {"kind": "attend_relationship_meeting", "invitation_id": invitation_id})
            invitation, = head().state.relationship_invitations
            assert [(record.actor_id, record.attended_tick) for record in invitation.meeting_attendance] == [
                (coworker_id, meeting_start), (ACTOR_ID, meeting_start),
            ]
            advance_to(meeting_end, "finish-appointment-window")
            assert head().state.resources[WORKER_ACCOUNT_ID].balance == Decimal("17.50")
            assert head().state.resources[EMPLOYER_ACCOUNT_ID].balance == Decimal("87.50")

            resumed = submit(f"{job_path}/control", {"action": "resume"}, "resume-original-work-window")
            assert resumed["status"] == "queued"
            status = client.get(job_path).raise_for_status().json()
            assert status["work_resume"] == {"skipped_shifts": 1, "next_shift_tick": str(next_shift)}
            final_job = run(work_id, lambda job: job.status is JobStatus.SUCCEEDED)
            assert final_job.payload["plan"] == authorized_plan
            assert final_job.committed_through_tick == next_shift + shift_length
            receipt = client.get(job_path).raise_for_status().json()["work_purchase_affordability"]
            assert receipt["listing_id"] == str(LISTING_ID)
            assert Decimal(receipt["total_price_minor"]) == Decimal("30")
            assert Decimal(receipt["wage_balance"]) == Decimal("30")
            assert receipt["confirmed_tick"] == str(next_shift + shift_length)

            before_purchase = head()
            assert before_purchase.state.resources[WORKER_ACCOUNT_ID].balance == Decimal("30")
            assert before_purchase.state.resources[EMPLOYER_ACCOUNT_ID].balance == Decimal("75")
            assert before_purchase.state.resources[buyer_goods_id].balance == 0
            assert before_purchase.state.resources[STOCK_ACCOUNT_ID].balance == 100
            assert before_purchase.state.resources[seller_money_id].balance == 0
            events = repository.list_events(scope, world_id, branch_id, limit=200).events
            paid = [event.tick for event in events if event.type == "core.work_shift_paid.v1"]
            assert paid == [first_shift + shift_length, next_shift + shift_length]

            purchase = action("现在确认买下这两件商品。", {
                "kind": "trade", "listing_id": str(LISTING_ID), "quantity": "2",
            })
            purchased = head()
            assert purchased.state.current_tick == before_purchase.state.current_tick + int(purchase["elapsed_ticks"])
            assert purchased.state.resources[WORKER_ACCOUNT_ID].balance == 0
            assert purchased.state.resources[seller_money_id].balance == Decimal("30")
            assert purchased.state.resources[buyer_goods_id].balance == 2
            assert purchased.state.resources[STOCK_ACCOUNT_ID].balance == 98
            assert models.calls == [RoleTask.INTENT] * 3
        finally:
            database.dispose()
