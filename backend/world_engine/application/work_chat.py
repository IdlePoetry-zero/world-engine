"""Typed intent and plan-based reply helpers for recurring work requests."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from decimal import Decimal
from typing import Annotated, Literal
from uuid import UUID

from pydantic import Field, StrictInt, model_validator

from world_engine.domain.components import DailyRoutineComponent, ExactDecimal, MarketComponent
from world_engine.domain.models import AccessScope, CalendarConfig, ContractModel, JobRecord, JobStatus, WorldState
from world_engine.domain.occupations import WorkAssignment, WorkPosting
from world_engine.domain.work_shift import WorkShiftError, WorkShiftTerms, registered_work_shift_terms
from world_engine.simulation.contracts import LongActionPlan, RoutineWorkSchedule, WorkPurchaseTarget
from world_engine.simulation.work_purchase import observe_work_purchase_quote

from .study_chat import MAX_SAFE_INTEGER, study_duration_seconds


_MIN_TICK = -(2**63)
_MAX_TICK = 2**63 - 1


class WorkIntentModel(ContractModel):
    """Closed immutable schema base for work chat intents."""


class WorkPurchaseTargetIntent(WorkIntentModel):
    """Player-selected listing and quantity for a savings target."""

    listing_id: UUID
    quantity: ExactDecimal = Field(gt=Decimal("0"))


class RoutineWorkScheduleRequest(WorkIntentModel):
    """Recurring shift selections before resolving world-owned data."""

    assignment_id: UUID
    first_shift_tick: Annotated[StrictInt, Field(ge=_MIN_TICK, le=_MAX_TICK)]
    repeat_every_days: Annotated[StrictInt, Field(gt=0, le=2**31 - 1)]
    balance_target: ExactDecimal | None = Field(
        default=None, ge=Decimal("0"), exclude_if=lambda value: value is None,
    )
    purchase_target: WorkPurchaseTargetIntent | None = Field(
        default=None, exclude_if=lambda value: value is None,
    )

    @model_validator(mode="after")
    def one_savings_target(self) -> RoutineWorkScheduleRequest:
        if self.balance_target is not None and self.purchase_target is not None:
            raise ValueError("a recurring work plan may have only one savings target")
        return self


class QueueWorkIntent(RoutineWorkScheduleRequest):
    """Explicit request to queue repeated shifts from an active assignment."""

    kind: Literal["queue_work"]
    amount: Annotated[StrictInt, Field(gt=0, le=MAX_SAFE_INTEGER)]
    unit: Literal["second", "hour", "day", "month", "year"]


class FollowWorkRoutineIntent(WorkIntentModel):
    """Explicit request to reuse the actor's saved daily work window."""

    kind: Literal["follow_work_routine"]
    assignment_id: UUID | None = Field(default=None, exclude_if=lambda value: value is None)
    amount: Annotated[StrictInt, Field(gt=0, le=MAX_SAFE_INTEGER)]
    unit: Literal["second", "hour", "day", "month", "year"]
    balance_target: ExactDecimal | None = Field(
        default=None, ge=Decimal("0"), exclude_if=lambda value: value is None,
    )
    purchase_target: WorkPurchaseTargetIntent | None = Field(
        default=None, exclude_if=lambda value: value is None,
    )

    @model_validator(mode="after")
    def one_savings_target(self) -> FollowWorkRoutineIntent:
        if self.balance_target is not None and self.purchase_target is not None:
            raise ValueError("a recurring work plan may have only one savings target")
        return self


class SavedWorkRoutineError(ValueError):
    """A missing, ambiguous, or conflicting saved work schedule condition."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def work_message_key(scope: AccessScope, request_id: UUID) -> str:
    """Return the account-scoped idempotency key for one work message."""

    return f"work-message:{scope.user_id}:{request_id}"


def work_queue_reply(request_id: UUID, job: JobRecord) -> dict:
    """Build a stable chat reply from the persisted work plan, not job progress."""

    if not isinstance(request_id, UUID):
        raise ValueError("A queued work reply requires a UUID request id.")
    if job.job_type != "simulation.long_action":
        raise ValueError("The referenced job is not a simulation long action.")
    plan = LongActionPlan.model_validate(job.payload.get("plan"))
    if plan.activity != "work" or plan.started_tick is None or plan.work_schedule is None:
        raise ValueError("The referenced long action is not a persisted recurring work plan.")
    duration = plan.target_tick - plan.started_tick
    if not 0 < duration <= MAX_SAFE_INTEGER:
        raise ValueError("The persisted work plan has an invalid duration.")
    schedule = plan.work_schedule
    target_note = (
        f"达到本人该岗位工资账户余额 {format(schedule.balance_target, 'f')} 时提前结束。"
        if schedule.balance_target is not None else ""
    )
    purchase_target = getattr(schedule, "purchase_target", None)
    references = [str(schedule.assignment_id)]
    if purchase_target is not None:
        target_note = (
            "达到所选商品的当前市场报价金额时提前结束；后续按当前报价重新核算，排队不会替你购买。"
        )
        references.append(str(purchase_target.listing_id))
    return {
        "message_id": str(request_id),
        "kind": "action",
        "content": (
            f"反复出勤作业已排队，时限为 {duration:,} 秒，首班世界时刻为 {schedule.first_shift_tick}，"
            f"此后每 {schedule.repeat_every_days} 个世界日重复一次。"
            "月份单位按 30 个世界日折算。"
            f"{target_note}本次排队没有推进世界时间。"
        ),
        "world_effect": False,
        "action_status": "succeeded",
        "job_id": str(job.job_id),
        "elapsed_ticks": "0",
        "references": references,
    }


def build_work_chat_plan(
    state: WorldState,
    actor_id: UUID,
    action: QueueWorkIntent,
    *,
    visible_market_offers: Sequence[Mapping[str, object]] = (),
) -> LongActionPlan:
    """Convert the player's time units and build a validated recurring work plan."""

    if not isinstance(state, WorldState):
        raise ValueError("A validated world state is required to plan work.")
    try:
        actor_id = UUID(str(actor_id))
        action = QueueWorkIntent.model_validate(action.model_dump(mode="python"))
    except (AttributeError, TypeError, ValueError) as error:
        raise ValueError("The recurring work request is invalid.") from error
    duration = _work_duration_seconds(action.amount, action.unit, state.calendar)
    return build_routine_work_plan(
        state, actor_id, action, duration, visible_market_offers=visible_market_offers,
    )


def build_routine_work_plan(
    state: WorldState,
    actor_id: UUID,
    selection: RoutineWorkScheduleRequest,
    duration_seconds: int,
    *,
    visible_market_offers: Sequence[Mapping[str, object]] = (),
) -> LongActionPlan:
    """Resolve the same schedule, workplace and savings rules for both entry points."""

    actor = state.entities.get(actor_id)
    if actor is None or actor.kind not in {"character", "person"} or actor.status != "active":
        raise ValueError("Only an active character can queue paid work.")

    assignments = [
        row for row in state.work_assignments
        if row.assignment_id == selection.assignment_id
        and row.actor_id == actor_id
        and row.ended_tick is None
    ]
    if len(assignments) != 1:
        raise ValueError("The work assignment is not an active assignment held by this character.")
    assignment = WorkAssignment.model_validate(assignments[0].model_dump(mode="python"))
    if actor.location_id != assignment.location_id:
        raise ValueError("The character must be at the assigned workplace to queue paid shifts.")
    if selection.first_shift_tick < max(state.current_tick, assignment.started_tick):
        raise WorkShiftError(
            "first_shift_outside_window", "The first paid shift cannot precede the current tick or active assignment.",
        )

    postings = [row for row in state.work_postings if row.posting_id == assignment.posting_id]
    if len(postings) != 1:
        raise ValueError("The assigned work posting is unavailable.")
    posting = WorkPosting.model_validate(postings[0].model_dump(mode="python"))
    if (
        posting.status != "open"
        or posting.employer_id != assignment.employer_id
        or posting.location_id != assignment.location_id
    ):
        raise ValueError("The active assignment no longer matches an open registered posting.")
    employer = state.entities.get(posting.employer_id)
    workplace = state.entities.get(posting.location_id)
    if employer is None or employer.kind not in {
        "organization", "polity", "faction", "company", "government",
    } or employer.status != "active":
        raise ValueError("The assigned employer is not an active organization.")
    if workplace is None or workplace.kind not in {"place", "location"} or workplace.status != "active":
        raise ValueError("The assigned workplace is not an active location.")
    terms = registered_work_shift_terms(state).get(posting.posting_id)
    if terms is None:
        raise WorkShiftError("terms_unregistered", "The assigned posting has no registered paid-shift terms.")

    if state.current_tick > _MAX_TICK - duration_seconds:
        raise ValueError("The work plan exceeds the supported world clock.")
    target_tick = state.current_tick + duration_seconds
    shift_end = selection.first_shift_tick + terms.duration_seconds
    if shift_end > target_tick:
        raise WorkShiftError(
            "first_shift_outside_window", "The first complete paid shift must fit within the requested duration.",
        )
    if selection.first_shift_tick < posting.valid_from_tick or (
        posting.valid_until_tick is not None and shift_end > posting.valid_until_tick
    ):
        raise ValueError("The first complete shift falls outside the posting validity window.")

    cadence_ticks = selection.repeat_every_days * state.calendar.day_seconds
    if cadence_ticks > _MAX_TICK or cadence_ticks < terms.duration_seconds:
        raise ValueError("The repeat interval must safely fit one registered paid shift.")
    purchase_target = None
    if selection.purchase_target is not None:
        try:
            purchase_target = build_work_purchase_target(
                state, actor_id, assignment, terms, selection.purchase_target, visible_market_offers,
            )
        except ValueError as error:
            raise WorkShiftError("purchase_target_unavailable", str(error)) from error
    schedule = RoutineWorkSchedule(
        assignment_id=assignment.assignment_id,
        first_shift_tick=selection.first_shift_tick,
        repeat_every_days=selection.repeat_every_days,
        balance_target=selection.balance_target,
        purchase_target=purchase_target,
    )
    return LongActionPlan(
        actor_id=actor_id,
        started_tick=state.current_tick,
        target_tick=target_tick,
        activity="work",
        work_schedule=schedule,
    )


def build_saved_work_routine_plan(
    state: WorldState,
    actor_id: UUID,
    action: FollowWorkRoutineIntent,
    *,
    explicit_text: str,
    visible_market_offers: Sequence[Mapping[str, object]] = (),
    existing_jobs: Sequence[JobRecord] = (),
) -> LongActionPlan:
    """Resolve one saved daily work window, then reuse the ordinary plan validator.

    A saved routine contributes only its next exact shift phase and a one-day
    cadence. The requested duration and optional savings target are the same
    trusted inputs used by explicit ``queue_work`` requests.
    """

    if not isinstance(state, WorldState):
        raise SavedWorkRoutineError("invalid_state")
    try:
        actor_id = UUID(str(actor_id))
        action = FollowWorkRoutineIntent.model_validate(action.model_dump(mode="python"))
    except (AttributeError, TypeError, ValueError) as error:
        raise SavedWorkRoutineError("invalid_request") from error
    actor = state.entities.get(actor_id)
    if actor is None or actor.kind not in {"character", "person"} or actor.status != "active":
        raise SavedWorkRoutineError("invalid_actor")

    active_assignments = sorted(
        (row for row in state.work_assignments if row.actor_id == actor_id and row.ended_tick is None),
        key=lambda row: str(row.assignment_id),
    )
    if not active_assignments:
        raise SavedWorkRoutineError("missing_assignment")
    if action.assignment_id is not None and not any(
        row.assignment_id == action.assignment_id for row in active_assignments
    ):
        raise SavedWorkRoutineError("invalid_assignment")

    routine_raw = actor.components.get("daily_routine")
    if routine_raw is None:
        raise SavedWorkRoutineError("missing_routine")
    try:
        routine = DailyRoutineComponent.model_validate(routine_raw)
        terms_by_posting = registered_work_shift_terms(state)
    except (TypeError, ValueError, WorkShiftError) as error:
        raise SavedWorkRoutineError("invalid_routine") from error

    eligible: list[tuple[WorkAssignment, object, LongActionPlan]] = []
    wrong_workplace = False
    saw_registered_work_window = False
    day_seconds = state.calendar.day_seconds
    for assignment in active_assignments:
        postings = [row for row in state.work_postings if row.posting_id == assignment.posting_id]
        if len(postings) != 1:
            continue
        posting = postings[0]
        terms = terms_by_posting.get(posting.posting_id)
        if terms is None:
            continue
        if actor.location_id != assignment.location_id:
            wrong_workplace = True
            continue
        for window in sorted(routine.windows, key=lambda row: str(row.window_id)):
            window_end = window.start_second + window.duration_seconds
            shift_phase = window_end - terms.duration_seconds
            if (
                window.activity != "work"
                or window.place_id != posting.location_id
                or window.duration_seconds < terms.duration_seconds
                or window_end > day_seconds
                or shift_phase < window.start_second
                or shift_phase + terms.duration_seconds != window_end
            ):
                continue
            saw_registered_work_window = True
            first_shift_tick = _next_daily_work_start(
                current_tick=state.current_tick,
                assignment_started_tick=assignment.started_tick,
                posting_valid_from_tick=posting.valid_from_tick,
                shift_phase=shift_phase,
                day_seconds=day_seconds,
            )
            if first_shift_tick is None:
                continue
            try:
                plan = build_work_chat_plan(
                    state,
                    actor_id,
                    QueueWorkIntent(
                        kind="queue_work",
                        assignment_id=assignment.assignment_id,
                        first_shift_tick=first_shift_tick,
                        repeat_every_days=1,
                        amount=action.amount,
                        unit=action.unit,
                        balance_target=action.balance_target,
                        purchase_target=action.purchase_target,
                    ),
                    visible_market_offers=visible_market_offers,
                )
            except ValueError:
                continue
            eligible.append((assignment, window, plan))

    if not eligible:
        if wrong_workplace:
            raise SavedWorkRoutineError("wrong_workplace")
        if not saw_registered_work_window:
            raise SavedWorkRoutineError("missing_work_window")
        raise SavedWorkRoutineError("schedule_unavailable")

    eligible_assignment_ids = {row.assignment_id for row, _window, _plan in eligible}
    if action.assignment_id is not None:
        selected = [row for row in eligible if row[0].assignment_id == action.assignment_id]
        if not selected:
            raise SavedWorkRoutineError("invalid_assignment")
        if len(eligible_assignment_ids) > 1 and not _explicitly_names_assignment(
            state, explicit_text, action.assignment_id, eligible,
        ):
            raise SavedWorkRoutineError("ambiguous_assignment")
    else:
        selected = eligible

    if len(selected) != 1:
        if len({row[0].assignment_id for row in selected}) > 1:
            raise SavedWorkRoutineError("ambiguous_assignment")
        raise SavedWorkRoutineError("ambiguous_work_window")

    assignment, _window, plan = selected[0]
    if _has_active_work_job(existing_jobs, actor_id, assignment.assignment_id):
        raise SavedWorkRoutineError("active_work_job")
    return plan


def _next_daily_work_start(
    *,
    current_tick: int,
    assignment_started_tick: int,
    posting_valid_from_tick: int,
    shift_phase: int,
    day_seconds: int,
) -> int | None:
    if type(day_seconds) is not int or day_seconds <= 0:
        return None
    earliest = max(current_tick, assignment_started_tick, posting_valid_from_tick)
    offset = (shift_phase - earliest) % day_seconds
    if earliest > _MAX_TICK - offset:
        return None
    return earliest + offset


def _explicitly_names_assignment(
    state: WorldState,
    text: str,
    selected_assignment_id: UUID,
    eligible: Sequence[tuple[WorkAssignment, object, LongActionPlan]],
) -> bool:
    normalized_text = _normalize_assignment_label(text)
    if not normalized_text:
        return False
    labels: dict[str, set[UUID]] = {}
    by_id = {assignment.assignment_id: assignment for assignment, _window, _plan in eligible}
    for assignment_id, assignment in by_id.items():
        posting = next((row for row in state.work_postings if row.posting_id == assignment.posting_id), None)
        workplace = state.entities.get(assignment.location_id)
        employer = state.entities.get(assignment.employer_id)
        titles = {assignment.title}
        if posting is not None:
            titles.add(posting.title)
        name_complements = {
            entity.name for entity in (workplace, employer)
            if entity is not None and entity.name
        }
        candidates = set(titles)
        candidates.update(
            f"{left} {right}"
            for title in titles
            for complement in name_complements
            for left, right in ((title, complement), (complement, title))
        )
        for label in candidates:
            normalized = _normalize_assignment_label(label)
            if len(normalized) >= 2:
                labels.setdefault(normalized, set()).add(assignment_id)
    named_assignments = {
        next(iter(assignment_ids))
        for label, assignment_ids in labels.items()
        if len(assignment_ids) == 1 and label in normalized_text
    }
    return named_assignments == {selected_assignment_id}


def _normalize_assignment_label(value: str) -> str:
    return "".join(character for character in value.casefold() if character.isalnum())


def _has_active_work_job(jobs: Sequence[JobRecord], actor_id: UUID, assignment_id: UUID) -> bool:
    """Best-effort duplicate-plan warning from the caller's recent job page.

    Durable duplicate-pay protection remains in the branch writer lease and
    the simulation worker's committed-tick/state-version fences.
    """
    active_statuses = {
        JobStatus.QUEUED, JobStatus.RUNNING, JobStatus.WAITING_INPUT, JobStatus.PAUSED,
    }
    for job in jobs:
        if (
            not isinstance(job, JobRecord)
            or job.job_type != "simulation.long_action"
            or job.status not in active_statuses
        ):
            continue
        try:
            plan = LongActionPlan.model_validate(job.payload.get("plan"))
        except (TypeError, ValueError):
            continue
        schedule = plan.work_schedule
        if (
            plan.activity == "work"
            and plan.actor_id == actor_id
            and schedule is not None
            and schedule.assignment_id == assignment_id
        ):
            return True
    return False


def build_work_purchase_target(
    state: WorldState,
    actor_id: UUID,
    assignment: WorkAssignment,
    terms: WorkShiftTerms,
    requested_target: WorkPurchaseTargetIntent,
    visible_market_offers: Sequence[Mapping[str, object]] = (),
) -> WorkPurchaseTarget:
    """Resolve a player-selected visible listing into trusted target terms.

    The offer allowlist is only used to prove the player saw the requested
    listing ID. Price, seller, currency, market, and resource are read from
    the validated state and the current quote is revalidated by simulation.
    """

    if not isinstance(state, WorldState):
        raise ValueError("A validated world state is required to resolve a purchase target.")
    try:
        actor_id = UUID(str(actor_id))
        assignment = WorkAssignment.model_validate(assignment.model_dump(mode="python"))
        terms = WorkShiftTerms.model_validate(terms.model_dump(mode="python"))
        requested = WorkPurchaseTargetIntent.model_validate(requested_target)
    except (AttributeError, TypeError, ValueError) as error:
        raise ValueError("The requested purchase savings target is invalid.") from error
    if not isinstance(visible_market_offers, Sequence) or isinstance(visible_market_offers, (str, bytes)):
        raise ValueError("Visible market offers must be an explicit listing allowlist.")
    visible_listing_ids: set[UUID] = set()
    for row in visible_market_offers:
        if not isinstance(row, Mapping):
            continue
        raw_id = row.get("listing_id")
        try:
            visible_listing_ids.add(UUID(str(raw_id)))
        except (TypeError, ValueError):
            continue
    if requested.listing_id not in visible_listing_ids:
        raise ValueError("The selected listing is not among the player's visible local market offers.")

    actor = state.entities.get(actor_id)
    if actor is None or actor.kind not in {"character", "person"} or actor.status != "active":
        raise ValueError("Only an active character can set a purchase savings target.")
    if assignment.actor_id != actor_id or assignment.ended_tick is not None:
        raise ValueError("The purchase target requires the actor's active work assignment.")
    assignments = [row for row in state.work_assignments if row.assignment_id == assignment.assignment_id]
    if len(assignments) != 1 or WorkAssignment.model_validate(assignments[0].model_dump(mode="python")) != assignment:
        raise ValueError("The purchase target assignment is not present in trusted world state.")
    if actor.location_id != assignment.location_id:
        raise ValueError("The actor must be at the assigned workplace to set a purchase target.")
    postings = [row for row in state.work_postings if row.posting_id == assignment.posting_id]
    if len(postings) != 1:
        raise ValueError("The purchase target assignment posting is not present in trusted world state.")
    posting = WorkPosting.model_validate(postings[0].model_dump(mode="python"))
    if (
        posting.status != "open"
        or posting.employer_id != assignment.employer_id
        or posting.location_id != assignment.location_id
    ):
        raise ValueError("The purchase target requires the active assignment's open registered posting.")
    employer = state.entities.get(posting.employer_id)
    if employer is None or employer.kind not in {
        "organization", "polity", "faction", "company", "government",
    } or employer.status != "active":
        raise ValueError("The purchase target requires an active registered employer.")
    registered_terms = registered_work_shift_terms(state).get(assignment.posting_id)
    if registered_terms is None or registered_terms != terms:
        raise ValueError("The purchase target requires the assignment's currently registered wage terms.")

    workplace = state.entities.get(assignment.location_id)
    if workplace is None or workplace.kind not in {"place", "location"} or workplace.status != "active":
        raise ValueError("The assigned workplace does not have an active local market.")
    try:
        market = MarketComponent.model_validate(workplace.components.get("market"))
    except (TypeError, ValueError) as error:
        raise ValueError("The assigned workplace does not have a valid local market.") from error
    matches = [offer for offer in market.offers if offer.listing_id == requested.listing_id and offer.active]
    if len(matches) != 1:
        raise ValueError("The selected listing is not an active offer at the assigned workplace.")
    offer = matches[0]
    seller = state.entities.get(offer.seller_id)
    if seller is None or seller.status != "active" or seller.location_id != workplace.id:
        raise ValueError("The selected listing has no active seller at the assigned workplace.")
    if terms.resource_id != market.currency_resource_id:
        raise ValueError("The work wage currency does not match the local market currency.")

    target = WorkPurchaseTarget(
        market_id=workplace.id,
        listing_id=offer.listing_id,
        resource_id=offer.resource_id,
        currency_resource_id=market.currency_resource_id,
        seller_id=offer.seller_id,
        quantity=requested.quantity,
    )
    observe_work_purchase_quote(state, target)
    return target


def _work_duration_seconds(amount: int, unit: str, calendar: CalendarConfig) -> int:
    """Use the established study conversion with an explicit 30-day month."""

    if unit == "month":
        if amount > MAX_SAFE_INTEGER // 30:
            raise ValueError("The requested month count exceeds the safe integer limit.")
        amount *= 30
        unit = "day"
    return study_duration_seconds(amount, unit, calendar)


