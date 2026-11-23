"""Commit one simulation window under a repository-owned job lease."""

from uuid import UUID, uuid5

from world_engine.adapters.models import DepthProfile, SpeedProfile, execution_profile
from world_engine.domain.errors import AccessDenied, Conflict, InsufficientStorage, InvalidLease, NotFound, StaleVersion, StorageBusy
from world_engine.domain.components import GovernanceComponent
from world_engine.domain.events import parse_event
from world_engine.domain.models import CommandEnvelope, JobRecord, JobStatus, MembershipRole, NewJob
from world_engine.domain.npc_plans import npc_journey_ids
from world_engine.domain.gameplay_events import GovernanceSuccessionEvent
from world_engine.domain.travel_itinerary import TravelItineraryArrivalProof
from world_engine.domain.travel_itinerary_progress import inspect_travel_itinerary_job
from world_engine.domain.travel_journey_events import TravelJourneyChangedEvent
from world_engine.domain.waypoint_opportunity import current_waypoint_opportunity
from world_engine.domain.waypoint_opportunity_events import (
    WAYPOINT_OPPORTUNITY_COMMAND, WaypointOpportunityDecision, WaypointOpportunityDecisionRecordedEvent,
    waypoint_opportunity_event_id, waypoint_opportunity_request_id, waypoint_opportunity_visibility,
)
from world_engine.domain.work_shift_events import WorkShiftPaidEvent
from world_engine.simulation import (
    LongActionPlan,
    SimulationContext,
    SimulationEngine,
    SimulationResolution,
    WorldAdvancePlan,
)
from world_engine.simulation.contracts import DecisionOption, DecisionRequest, DecisionResponse, ResolutionMetrics


_NPC_REFLECTION_SOURCE_TYPES = frozenset({
    "core.npc_plan_revised.v1", "core.npc_plan_review_resolved.v1",
    "core.knowledge_acquired.v1", "core.investigation_case_closed.v1",
})


class SimulationJobs:
    def __init__(self, repository, engine=None):
        self.repository = repository
        self.engine = engine if engine is not None else SimulationEngine()

    def enqueue(self, scope, world_id, branch_id, plan, *, idempotency_key, speed, depth,
                expected_state_version=None):
        view = self.repository.read_world(scope, world_id, branch_id)
        if expected_state_version is not None and view.branch.state_version != expected_state_version:
            raise StaleVersion(expected_state_version, view.branch.state_version)
        if isinstance(plan, WorldAdvancePlan):
            self._authorize_world_advance(scope, view)
            if plan.target_tick <= view.state.current_tick:
                raise Conflict("The target must be later than the branch clock.")
            if plan.started_tick != view.state.current_tick:
                raise Conflict("World advancement must start at the reviewed branch tick.")
            job_type = "simulation.world_advance"
        elif isinstance(plan, LongActionPlan):
            self._authorize_actor(scope, view, plan.actor_id)
            actor_status = view.state.entities[plan.actor_id].status
            if actor_status in {"dead", "retired"}:
                raise Conflict("A terminal character cannot begin a long action.")
            if actor_status == "incapacitated":
                raise Conflict("An incapacitated character cannot begin a long action.")
            if plan.target_tick <= view.state.current_tick:
                raise Conflict("The target must be later than the branch clock.")
            if plan.started_tick != view.state.current_tick:
                raise Conflict("The long action must start at the reviewed branch tick.")
            job_type = "simulation.long_action"
        else:
            raise TypeError("SimulationJobs requires a validated LongActionPlan or WorldAdvancePlan.")
        payload = {"plan": plan.model_dump(mode="json"), "speed": speed, "depth": depth,
                   "initial_state_version": view.branch.state_version}
        existing = self.repository.find_job_by_key(scope, world_id, branch_id, idempotency_key)
        profile = execution_profile(SpeedProfile(speed), DepthProfile(depth)) if existing is None else None
        if profile is not None:
            payload["npc_reflection_mode"] = profile.reflection_mode
        elif "npc_reflection_mode" in existing.payload:
            payload["npc_reflection_mode"] = existing.payload["npc_reflection_mode"]
        if isinstance(plan, WorldAdvancePlan):
            if existing is not None and "max_steady_macro_days" in existing.payload:
                payload["max_steady_macro_days"] = existing.payload["max_steady_macro_days"]
            elif profile is not None:
                payload["max_steady_macro_days"] = profile.max_steady_macro_days
        if isinstance(plan, LongActionPlan) and (
            plan.activity == "work" or plan.travel_itinerary is not None
        ):
            payload["submitted_by"] = str(scope.user_id)
        if isinstance(plan, LongActionPlan) and plan.activity == "study":
            # A preexisting unstamped job must retain the payload used for its idempotency hash.
            if existing is None or "submitted_by" in existing.payload:
                payload["submitted_by"] = str(scope.user_id)
        return self.repository.enqueue_job(scope, NewJob(
            world_id=world_id, branch_id=branch_id, job_type=job_type,
            idempotency_key=idempotency_key,
            payload=payload,
        ), expected_state_version=view.branch.state_version)

    def enqueue_world_advance(self, scope, world_id, branch_id, plan, **kwargs):
        """Explicit application entry point for owner-only observer advancement."""
        if not isinstance(plan, WorldAdvancePlan):
            raise TypeError("enqueue_world_advance requires a WorldAdvancePlan.")
        return self.enqueue(scope, world_id, branch_id, plan, **kwargs)

    def advance(self, scope, lease, *, model_decision=None) -> SimulationResolution | JobRecord:
        job = self.repository.read_job(scope, lease.job_id)
        if job.job_type not in {"simulation.long_action", "simulation.world_advance"}:
            raise Conflict("This worker only handles registered simulation jobs.")
        view = self.repository.read_world(scope, lease.world_id, lease.branch_id)
        if job.job_type == "simulation.world_advance":
            self._authorize_world_advance(scope, view)
        if job.committed_through_tick != view.state.current_tick:
            if self._has_pending_waypoint_answer(job):
                return self._pause_stale_waypoint(lease)
            return self.repository.finish_job(
                lease, JobStatus.FAILED, result={"error": "BRANCH_CLOCK_CHANGED"},
            )
        return self._advance_owned_job(scope, lease, job, view, model_decision)

    def _advance_owned_job(self, scope, lease, job, view, model_decision):
        try:
            return self._plan_and_commit(scope, lease, job, view, model_decision)
        except InsufficientStorage:
            # A window automatically rolls back when SQLite cannot grow. If even
            # the pause record cannot fit, the original lease remains recoverable.
            try:
                return self.repository.finish_job(
                    lease, JobStatus.PAUSED, result={"reason_code": "insufficient_storage"},
                )
            except (InsufficientStorage, InvalidLease):
                pass
            raise
        except (InvalidLease, StorageBusy):
            # A locked transaction is recoverable with the same fence, or
            # after lease expiry. It does not make the scheduled action fail.
            raise
        except Exception:
            # Record a safe status while the lease is still current. Keep the
            # original exception available to the worker diagnostic boundary.
            try:
                self.repository.finish_job(lease, JobStatus.FAILED, result={"error": "SIMULATION_WINDOW_FAILED"})
            except InvalidLease:
                pass
            raise

    def _plan_and_commit(self, scope, lease, job, view, model_decision):
        if model_decision is not None:
            recorded = self.repository.record_model_decision(
                lease, model_decision, expected_state_version=view.branch.state_version,
            )
            if recorded == "committed":
                return self.repository.read_job(scope, job.job_id)
            job = self.repository.read_job(scope, job.job_id)
        if model_decision is None and job.payload.get("pending_npc_reflection") is not None:
            return self._advance_npc_reflection(scope, lease, job, view.state.current_tick)
        if job.job_type == "simulation.world_advance":
            plan = WorldAdvancePlan.model_validate(job.payload["plan"])
        else:
            plan = LongActionPlan.model_validate(job.payload["plan"])
            effective_target = job.payload.get("effective_travel_target_tick")
            if plan.activity == "travel" and effective_target is not None and plan.travel_itinerary is None:
                plan = plan.model_copy(update={"target_tick": effective_target})
        initial_version = job.payload.get("initial_state_version")
        if plan.started_tick is None or initial_version is None:
            raise Conflict("The simulation job has no reviewed starting state.")
        committed_version = job.payload.get("committed_state_version")
        if committed_version is None and view.state.current_tick == plan.started_tick:
            committed_version = initial_version
        if committed_version is not None and view.branch.state_version != committed_version:
            if self._has_pending_waypoint_answer(job):
                return self._pause_stale_waypoint(lease)
            return self.repository.finish_job(
                lease, JobStatus.FAILED, result={"error": "STALE_START_VERSION"},
            )
        if isinstance(plan, WorldAdvancePlan):
            self._authorize_world_advance(scope, view)
            command_actor_id = scope.user_id
        else:
            self._authorize_actor(scope, view, plan.actor_id)
            command_actor_id = plan.actor_id
        itinerary = plan.travel_itinerary if isinstance(plan, LongActionPlan) else None
        itinerary_step = None
        itinerary_completed = False
        engine_plan = plan
        itinerary_context = {}
        command_id = uuid5(job.job_id, f"window:{view.branch.state_version}")
        pending_decision = job.payload.get("pending_decision")
        decision = DecisionResponse.model_validate(pending_decision) if pending_decision else model_decision
        if isinstance(plan, LongActionPlan) and plan.work_schedule is not None and "work_resume" in job.payload:
            resume_tick = job.payload["work_resume"]["next_shift_tick"]
            schedule = plan.work_schedule.model_copy(update={
                "first_shift_tick": int(resume_tick) if resume_tick is not None else plan.target_tick,
            })
            engine_plan = plan.model_copy(update={"work_schedule": schedule})
        if itinerary is not None:
            raw_cursor = job.payload.get("itinerary_cursor")
            cursor = TravelItineraryArrivalProof.model_validate(raw_cursor) if raw_cursor is not None else None
            itinerary_step = inspect_travel_itinerary_job(view.state, itinerary, cursor=cursor)
            opportunity = current_waypoint_opportunity(view.state, itinerary, cursor)
            if opportunity is not None:
                return self._resolve_waypoint_opportunity(
                    scope, lease, job, view, plan, cursor, opportunity, command_id, decision,
                )
            itinerary_completed = itinerary_step.outcome == "completed"
            if itinerary_step.outcome == "blocked_interrupted":
                return self.repository.finish_job(
                    lease, JobStatus.PAUSED, committed_through_tick=view.state.current_tick,
                    result={"reason_code": itinerary_step.reason_code},
                )
            if not itinerary_completed:
                leg_parameters = plan.model_dump(mode="python", exclude={"itinerary_auto_care"})
                remaining_legs = itinerary.legs[itinerary_step.leg_index + 1:]
                leg_deadline = itinerary.deadline_tick - sum(leg.travel_ticks for leg in remaining_legs)
                engine_plan = LongActionPlan.model_validate(leg_parameters | {
                    "travel_itinerary": None, "route_id": itinerary_step.leg.route_id, "target_tick": leg_deadline,
                })
                itinerary_context["itinerary_journey_id"] = itinerary_step.journey_id
                itinerary_context["itinerary_auto_care"] = plan.itinerary_auto_care
        deceased_officeholders = set()
        if decision is not None:
            for entity in view.state.entities.values():
                raw = entity.components.get("governance")
                if raw is None:
                    continue
                governance = GovernanceComponent.model_validate(raw)
                holders = [governance.leader_id, *(office.holder_id for office in governance.offices)]
                deceased_officeholders.update(
                    holder for holder in holders if holder in view.state.entities
                    and view.state.entities[holder].status in {"dead", "retired"}
                )
        causal_deaths = {}
        if deceased_officeholders:
            causal_deaths = self.repository.find_death_events(
                scope, job.world_id, job.branch_id, deceased_officeholders,
            )
        world_seed = view.state.metadata.get("generation_seed")
        if world_seed is None:
            world_seed = view.state.metadata.get("seed")
        if world_seed is None or world_seed == "":
            world_seed = job.world_id
        controlled_actor_ids = (
            self.repository.world_actor_control_ids(scope, job.world_id)
            if isinstance(plan, WorldAdvancePlan) or view.world.owner_id == scope.user_id
            else frozenset()
        )
        journey_ids = npc_journey_ids(view.state)
        journey_receipts = self.repository.find_journey_resolution_events(
            scope, job.world_id, job.branch_id, journey_ids,
        ) if journey_ids else {}
        simulation_context = SimulationContext(
            command_id=command_id,
            world_id=job.world_id,
            branch_id=job.branch_id,
            ruleset_version=view.world.ruleset_version,
            world_seed=str(world_seed),
            state_version=view.branch.state_version,
            speed=job.payload.get("speed", "balanced"),
            depth=job.payload.get("depth", "deep"),
            max_steady_macro_days=(
                job.payload.get("max_steady_macro_days")
                if isinstance(plan, WorldAdvancePlan) else None
            ),
            command_actor_id=command_actor_id,
            externally_controlled_actor_ids=controlled_actor_ids,
            allow_autonomous_vacancy=view.world.owner_id == scope.user_id,
            allow_autonomous_meeting_attendance=view.world.owner_id == scope.user_id,
            allow_npc_routine_mobility=view.world.owner_id == scope.user_id,
            work_job_id=job.job_id if isinstance(plan, LongActionPlan) and plan.work_schedule is not None else None,
            decision_responses=(decision,) if decision is not None else (),
            causal_death_event_ids=causal_deaths,
            journey_resolution_receipts=journey_receipts,
            journey_receipt_branch_ids=frozenset(receipt.branch_id for receipt in journey_receipts.values()),
            **itinerary_context,
        )
        simulation_context._itinerary_transit_projection_binding = (
            lease.job_id, lease.worker_id, lease.fencing_token, view.branch.state_hash,
        )
        review_after_arrival = getattr(self.engine, "resolve_autonomous_vacancy", None)
        if itinerary_completed:
            resolution = review_after_arrival(view.state, engine_plan, simulation_context) if review_after_arrival else None
            meeting_after_arrival = getattr(self.engine, "resolve_autonomous_meeting_attendance", None)
            if resolution is None and meeting_after_arrival is not None:
                resolution = meeting_after_arrival(view.state, engine_plan, simulation_context)
            if resolution is None:
                return self.repository.finish_job(
                    lease, JobStatus.SUCCEEDED, committed_through_tick=view.state.current_tick,
                    result={"reason_code": "travel_itinerary_completed"},
                )
        else:
            resolution = self.engine.plan_window(view.state, engine_plan, simulation_context)
        if (resolution.outcome == "decision_required" and not resolution.events
            and len(resolution.decision_requests) == 1
            and resolution.decision_requests[0].options
            and resolution.decision_requests[0].decision_owner == "model"
            and decision is None):
            return resolution
        itinerary_arrived = False
        if itinerary_step is not None and resolution.outcome == "completed":
            itinerary_arrived = any(
                isinstance(event, TravelJourneyChangedEvent) and event.operation == "arrive"
                and event.journey.journey_id == itinerary_step.journey_id
                for event in resolution.events
            )
            if not itinerary_arrived:
                raise Conflict("The itinerary worker cannot complete a leg without its departure and arrival.")
        if itinerary_arrived:
            more_legs = itinerary_step.leg_index + 1 < len(itinerary.legs)
            review_due = False
            if not more_legs and review_after_arrival and simulation_context.allow_autonomous_vacancy:
                from world_engine.simulation.autonomous_vacancy import next_autonomous_vacancy_step

                review_due = next_autonomous_vacancy_step(view.state, simulation_context) is not None
            if more_legs or review_due:
                resolution = resolution.model_copy(update={"outcome": "advanced", "continuation": plan})
        status = {
            "completed": JobStatus.SUCCEEDED,
            "stopped": JobStatus.PAUSED,
            "decision_required": JobStatus.WAITING_INPUT,
            "rule_required": JobStatus.PAUSED,
        }.get(resolution.outcome)
        if status is JobStatus.PAUSED and resolution.interruption is not None:
            if resolution.interruption.reason_code in {"actor_lifespan_expired", "died_of_deprivation"}:
                status = JobStatus.FAILED
        if status is JobStatus.WAITING_INPUT and not any(
            pending.decision_owner == "user" and pending.options
            for pending in resolution.decision_requests
        ):
            status = JobStatus.PAUSED
        capture_history_context = (
            job.job_type == "simulation.long_action"
            and status in {JobStatus.SUCCEEDED, JobStatus.WAITING_INPUT}
            and bool(resolution.events)
        )
        capture_npc_reflection = (
            "npc_reflection_mode" in job.payload
            and status in {None, JobStatus.SUCCEEDED}
            and any(event.type in _NPC_REFLECTION_SOURCE_TYPES for event in resolution.events)
        )
        commit_result = None
        if resolution.events:
            causes = list(dict.fromkeys(
                event.source_death_event_id for event in resolution.events
                if isinstance(event, GovernanceSuccessionEvent)
            ))
            paid_shift = None
            if isinstance(plan, LongActionPlan) and plan.activity == "work":
                paid_shift = next((event for event in resolution.events if isinstance(event, WorkShiftPaidEvent)), None)
            command_type = "simulation.window"
            command_payload = {"job_id": str(job.job_id), "next_tick": str(resolution.next_tick)}
            if resolution.metrics.integration_boundary == "npc_routine_departed":
                command_actor_id = scope.user_id
            if resolution.metrics.integration_boundary == "autonomous_vacancy_processed":
                from world_engine.domain.leadership_ballot_state import AUTONOMOUS_VACANCY_COMMAND, autonomous_vacancy_command_payload

                command_type = AUTONOMOUS_VACANCY_COMMAND
                command_payload = autonomous_vacancy_command_payload(resolution.events)
                command_actor_id = scope.user_id
            if resolution.metrics.integration_boundary == "npc_meeting_attendance_resolved":
                from world_engine.domain.npc_meeting_attendance import (
                    NPC_MEETING_ATTENDANCE_COMMAND, npc_meeting_attendance_command_payload,
                )

                command_type = NPC_MEETING_ATTENDANCE_COMMAND
                command_payload = npc_meeting_attendance_command_payload(resolution.events[-1].record)
                command_actor_id = scope.user_id
            if paid_shift is not None:
                command_type = "action.work_shift"
                command_payload = {"kind": "work_shift", "assignment_id": str(paid_shift.plan.assignment_id)}
            commit_kwargs = {"lease": lease}
            itinerary_care_receipt = resolution._itinerary_care_receipt
            if itinerary_care_receipt is not None:
                commit_kwargs["itinerary_care_receipt"] = itinerary_care_receipt
            itinerary_transit_projection_receipt = resolution._itinerary_transit_projection_receipt
            if itinerary_transit_projection_receipt is not None:
                commit_kwargs["itinerary_transit_projection_receipt"] = itinerary_transit_projection_receipt
            if capture_history_context:
                commit_kwargs["capture_player_history_candidate"] = True
            if capture_npc_reflection:
                commit_kwargs["capture_npc_reflection"] = True
            commit_result = self.repository.commit_command(scope, job.world_id, job.branch_id, CommandEnvelope(
                command_id=command_id, actor_id=command_actor_id,
                idempotency_key=f"simulation:{command_id}",
                expected_state_version=view.branch.state_version,
                command_type=command_type,
                causation_ids=causes,
                payload=command_payload,
            ), resolution.events, **commit_kwargs)
        if status is JobStatus.SUCCEEDED and capture_npc_reflection:
            updated_job = self.repository.read_job(scope, job.job_id)
            if updated_job.payload.get("pending_npc_reflection") is not None:
                if capture_history_context:
                    self.repository.discard_terminal_player_history_context_candidate(job.job_id)
                # The final source is already durable. Resolve its zero-time
                # follow-up before finishing the saved plan on its next pass.
                return resolution.model_copy(update={"outcome": "advanced", "continuation": plan})
        if (status is JobStatus.SUCCEEDED and commit_result is not None
                and simulation_context.allow_autonomous_meeting_attendance
                and (not isinstance(plan, LongActionPlan) or plan.consumption is None)
                and any(row.status == "accepted" and row.meeting is not None
                        and row.meeting.start_tick <= resolution.next_tick < row.meeting.end_tick
                        for row in view.state.relationship_invitations)):
            from world_engine.simulation.npc_meeting_attendance import next_npc_meeting_attendance_candidate

            after = self.repository.read_world(scope, job.world_id, job.branch_id)
            excluded = controlled_actor_ids | ({plan.actor_id} if isinstance(plan, LongActionPlan) else set())
            if next_npc_meeting_attendance_candidate(after.state, externally_controlled_actor_ids=excluded) is not None:
                if capture_history_context:
                    self.repository.discard_terminal_player_history_context_candidate(job.job_id)
                return resolution.model_copy(update={"outcome": "advanced", "continuation": plan})
        if status is not None:
            result_fields = resolution.model_dump(mode="json", exclude={"events"})
            if itinerary is not None and status is JobStatus.SUCCEEDED:
                result_fields["reason_code"] = "travel_itinerary_completed"
            try:
                finished = self.repository.finish_job(
                    lease, status, committed_through_tick=resolution.next_tick,
                    result=result_fields,
                )
            except Exception:
                if capture_history_context:
                    self.repository.discard_terminal_player_history_context_candidate(job.job_id)
                raise
            if capture_history_context and commit_result is not None:
                self.repository.publish_terminal_player_history_context_candidate(scope, finished)
        return resolution

    def _advance_npc_reflection(self, scope, lease, job, tick):
        pending = self.repository.read_pending_npc_reflection_source(scope, lease)
        if pending is None:
            raise Conflict("The leased NPC reflection no longer has a pending source.")
        requests = ()
        events = ()
        if pending["result"] is None:
            requests = (DecisionRequest(
                request_id=UUID(pending["request_id"]), kind="npc.reflection", actor_id=UUID(pending["actor_id"]),
                causal_subject_id=UUID(pending["source_event_id"]), at_tick=pending["tick"],
                state_version=pending["source_state_version"], title="人物反思", reason_code="npc_reflection_due",
                options=(DecisionOption(option_id="reflect", label="由人物反思", effect_summary="人物可依据本人经历形成一条私有记忆。"),),
            ),)
        else:
            committed = self.repository.commit_pending_npc_reflection(scope, lease)
            if committed is not None:
                events = tuple(parse_event(event.payload | {
                    "event_id": event.event_id, "type": event.type, "visibility": event.visibility,
                }) for event in committed.events)
        return SimulationResolution(
            outcome="decision_required" if requests else "advanced", events=events, next_tick=tick,
            decision_requests=requests,
            metrics=ResolutionMetrics(
                from_tick=tick, to_tick=tick, ticks_advanced=0, event_count=len(events),
                integration_boundary="npc_reflection_due" if requests else "npc_reflection_resolved",
                speed=job.payload["speed"], depth=job.payload["depth"],
            ),
        )

    def _resolve_waypoint_opportunity(self, scope, lease, job, view, plan, cursor, opportunity, command_id, decision):
        tick = view.state.current_tick
        itinerary = plan.travel_itinerary
        actor_id = itinerary.actor_id
        request_id = waypoint_opportunity_request_id(cursor.journey_id, opportunity.opportunity_id, view.branch.state_version)
        request = DecisionRequest(
            request_id=request_id, kind="waypoint_opportunity", decision_owner="user", actor_id=actor_id,
            at_tick=tick, state_version=view.branch.state_version, title=opportunity.title,
            reason_code="waypoint_opportunity",
            options=(
                DecisionOption(option_id="stop", label="在此停留", effect_summary=opportunity.summary),
                DecisionOption(option_id="continue", label="继续原行程", effect_summary="按原路线继续，不在这里停留。"),
            ),
        )
        if decision is None:
            return self.repository.finish_job(lease, JobStatus.WAITING_INPUT, committed_through_tick=tick, result={
                "reason_code": "waypoint_opportunity", "decision_requests": [request.model_dump(mode="json")],
            })
        if (decision.request_id, decision.state_version, decision.at_tick, decision.source) != (
            request_id, view.branch.state_version, tick, "user",
        ) or decision.option_id not in {"continue", "stop"}:
            raise Conflict("The waypoint opportunity requires its exact current player response.")
        event = WaypointOpportunityDecisionRecordedEvent(
            event_id=waypoint_opportunity_event_id(cursor.journey_id), world_id=job.world_id,
            branch_id=job.branch_id, command_id=command_id, actor_id=actor_id,
            ruleset_version=view.world.ruleset_version, visibility=waypoint_opportunity_visibility(actor_id),
            record=WaypointOpportunityDecision(
                journey_id=cursor.journey_id, actor_id=actor_id, opportunity_id=opportunity.opportunity_id,
                at_tick=tick, itinerary_started_tick=itinerary.created_tick,
                state_version=view.branch.state_version, choice=decision.option_id,
            ),
        )
        self.repository.commit_command(scope, job.world_id, job.branch_id, CommandEnvelope(
            command_id=command_id, actor_id=actor_id, idempotency_key=f"simulation:{command_id}",
            expected_state_version=view.branch.state_version, command_type=WAYPOINT_OPPORTUNITY_COMMAND,
            payload={"request_id": str(request_id), "option_id": decision.option_id},
        ), (event,), lease=lease)
        if decision.option_id == "stop":
            return self.repository.read_job(scope, job.job_id)
        return SimulationResolution(
            outcome="advanced", events=(event,), next_tick=tick, continuation=plan,
            accepted_decisions=(decision,), metrics=ResolutionMetrics(
                from_tick=tick, to_tick=tick, ticks_advanced=0,
                integration_boundary="waypoint_opportunity", event_count=1,
                speed=job.payload["speed"], depth=job.payload["depth"],
            ),
        )

    @staticmethod
    def _has_pending_waypoint_answer(job):
        return bool(job.payload.get("pending_decision")) and (job.result or {}).get("reason_code") == "waypoint_opportunity"

    def _pause_stale_waypoint(self, lease):
        return self.repository.finish_job(lease, JobStatus.PAUSED, result={"reason_code": "waypoint_opportunity_stale"})

    def _authorize_actor(self, scope, view, actor_id):
        actor = view.state.entities.get(actor_id)
        if actor is None or actor.kind not in {"character", "person"}:
            raise NotFound("Actor does not exist in this branch.")
        self.repository.authorize_actor_control(scope, view.world.world_id, actor_id)

    def _authorize_world_advance(self, scope, view):
        if view.world.owner_id != scope.user_id:
            raise AccessDenied("Only the canonical world owner can advance the whole world.")
        self.repository.authorize_role(scope, view.world.world_id, {MembershipRole.OWNER})
