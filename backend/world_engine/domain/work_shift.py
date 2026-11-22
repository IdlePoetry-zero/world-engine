"""Pure, bounded planning for one already-completed paid work shift.

This module does not advance the world clock or emit an event. A trusted host
may build the plan after the simulation has reached the registered shift end,
then embed its exact compare-and-swap inputs in the atomic settlement command.
The host must keep the shift terms in its registered per-posting policy and
must never take wage or account selectors from a model proposal.
"""

from __future__ import annotations

from decimal import Decimal, localcontext
from typing import TYPE_CHECKING, Annotated
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StrictInt, model_validator

from .components import ExactDecimal, InventoryComponent, ResourceName, TreasuryComponent
from .occupations import WorkAssignment, WorkPosting

if TYPE_CHECKING:
    from .models import ResourceAccount, WorldEntity, WorldState

MAX_WORK_SHIFT_SECONDS = 86_400
_MIN_TICK = -(2**63)
_MAX_TICK = 2**63 - 1
_MAX_REVISION = 2**63 - 1
_ORGANIZATION_KINDS = frozenset({"organization", "polity", "faction", "company", "government"})
Tick = Annotated[int, Field(strict=True, ge=_MIN_TICK, le=_MAX_TICK)]
PositiveBalance = Annotated[ExactDecimal, Field(ge=Decimal("0"))]


class WorkShiftError(ValueError):
    """A stable eligibility, funds, or stale-plan error for work settlement."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


class WorkShiftModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        str_strip_whitespace=True,
        validate_assignment=True,
        allow_inf_nan=False,
    )


class WorkShiftTerms(WorkShiftModel):
    """Host-registered terms for a single posting and one bounded shift."""

    posting_id: UUID
    duration_seconds: StrictInt = Field(ge=1, le=MAX_WORK_SHIFT_SECONDS)
    resource_id: ResourceName
    wage: ExactDecimal = Field(gt=Decimal("0"))


class WorkShiftLedgerEntry(WorkShiftModel):
    """One exact holder-account side of the balanced wage transfer."""

    account_id: UUID
    owner_id: UUID
    resource_id: ResourceName
    amount: ExactDecimal
    balance_before: PositiveBalance
    balance_after: PositiveBalance

    @model_validator(mode="after")
    def balance_matches_posting(self) -> WorkShiftLedgerEntry:
        with localcontext() as context:
            context.prec = 40
            expected = self.balance_before + self.amount
        if expected != self.balance_after:
            raise ValueError("work-shift ledger amount must exactly reconcile before and after balances")
        return self


class PaidWorkShiftPlan(WorkShiftModel):
    """Immutable, host-revalidatable completion and double-entry CAS plan.

    ``ledger_entries`` have a stable order: worker credit first, employer
    debit second. Their amounts must sum to zero and their account IDs must be
    distinct. Revalidation recomputes this entire DTO from current world state.
    """

    terms: WorkShiftTerms
    actor_id: UUID
    employer_id: UUID
    workplace_id: UUID
    assignment_id: UUID
    posting_revision: StrictInt = Field(ge=1, le=_MAX_REVISION)
    assignment_revision: StrictInt = Field(ge=1, le=_MAX_REVISION)
    shift_started_tick: Tick
    completed_tick: Tick
    ledger_entries: tuple[WorkShiftLedgerEntry, WorkShiftLedgerEntry]

    @model_validator(mode="after")
    def validates_shift_and_double_entry(self) -> PaidWorkShiftPlan:
        if self.actor_id == self.employer_id:
            raise ValueError("work-shift worker and employer must be distinct entities")
        if self.completed_tick - self.shift_started_tick != self.terms.duration_seconds:
            raise ValueError("work-shift plan ticks must match the registered duration")
        worker_entry, employer_entry = self.ledger_entries
        if worker_entry.account_id == employer_entry.account_id:
            raise ValueError("work-shift worker and employer accounts must be distinct")
        if (
            worker_entry.owner_id != self.actor_id
            or employer_entry.owner_id != self.employer_id
            or worker_entry.resource_id != self.terms.resource_id
            or employer_entry.resource_id != self.terms.resource_id
        ):
            raise ValueError("work-shift ledger accounts must match their parties and registered resource")
        if worker_entry.amount != self.terms.wage or employer_entry.amount != self.terms.wage.copy_negate():
            raise ValueError("work-shift ledger entries must transfer exactly the registered wage")
        with localcontext() as context:
            context.prec = 40
            total = worker_entry.amount + employer_entry.amount
        if total != 0:
            raise ValueError("work-shift ledger postings must conserve the registered resource")
        return self


def select_wage_holder_account(state: WorldState, worker_id: UUID, resource_id: str) -> UUID | None:
    """Choose the canonical worker-owned account for an authored wage currency."""
    return min(
        (account_id for account_id, account in state.resources.items()
         if account.owner_id == worker_id and account.account_type == "holder" and account.resource_id == resource_id),
        key=str,
        default=None,
    )


def plan_paid_work_shift(
    state: WorldState,
    *,
    terms: WorkShiftTerms,
    actor_id: UUID,
    assignment_id: UUID,
    worker_account_id: UUID,
    shift_started_tick: int,
    expected_posting_revision: int,
    expected_assignment_revision: int,
) -> PaidWorkShiftPlan:
    """Build the exact settlement plan for a shift that has reached its end.

    ``worker_account_id`` is an internal host selection, never a model action
    field. The employer account is resolved from the employer's registered
    treasury for ``terms.resource_id``. The world must already be at the exact
    completion tick; this function itself never changes time or balances.
    """

    state = _checked_state(state)
    terms = WorkShiftTerms.model_validate(terms.model_dump(mode="python"))
    registered_terms = registered_work_shift_terms(state).get(terms.posting_id)
    if registered_terms is None:
        raise WorkShiftError("terms_unregistered", "This posting has no registered paid-shift terms.")
    if registered_terms != terms:
        raise WorkShiftError("registered_terms_changed", "The supplied terms do not match the posting's registered terms.")
    actor_id = _uuid(actor_id, "actor")
    assignment_id = _uuid(assignment_id, "assignment")
    worker_account_id = _uuid(worker_account_id, "worker account")
    shift_started_tick = _tick(shift_started_tick, "shift start")
    _revision(expected_posting_revision, "posting")
    _revision(expected_assignment_revision, "assignment")

    posting = _one_posting(state, terms.posting_id)
    assignment = _one_assignment(state, assignment_id)
    if posting.revision != expected_posting_revision:
        raise WorkShiftError("stale_posting_revision", "The work posting changed before settlement.")
    if assignment.revision != expected_assignment_revision:
        raise WorkShiftError("stale_assignment_revision", "The employment assignment changed before settlement.")
    if (
        assignment.actor_id != actor_id
        or assignment.posting_id != posting.posting_id
        or assignment.employer_id != posting.employer_id
        or assignment.location_id != posting.location_id
        or assignment.ended_tick is not None
    ):
        raise WorkShiftError("assignment_not_current", "This character no longer holds the active posting assignment.")
    if posting.status != "open":
        raise WorkShiftError("posting_closed", "The work posting is closed.")
    if actor_id == posting.employer_id:
        raise WorkShiftError("parties_not_distinct", "A worker cannot receive wages from their own employer account.")

    duration = terms.duration_seconds
    if shift_started_tick > _MAX_TICK - duration:
        raise WorkShiftError("tick_overflow", "The work shift exceeds the world clock range.")
    completed_tick = shift_started_tick + duration
    if state.current_tick != completed_tick:
        raise WorkShiftError("completion_tick_mismatch", "The world must be at the exact end of this work shift.")
    if assignment.started_tick > shift_started_tick:
        raise WorkShiftError("assignment_not_started", "The employment assignment did not exist when the shift began.")
    if shift_started_tick < posting.valid_from_tick or (
        posting.valid_until_tick is not None and
        (shift_started_tick >= posting.valid_until_tick or completed_tick > posting.valid_until_tick)
    ):
        raise WorkShiftError("posting_out_of_date", "The registered posting does not cover the complete shift.")

    actor = _entity(state, actor_id, "worker")
    employer = _entity(state, posting.employer_id, "employer")
    workplace = _entity(state, posting.location_id, "workplace")
    if actor.kind not in {"character", "person"} or actor.status != "active":
        raise WorkShiftError("actor_inactive", "Only an active character can complete a paid shift.")
    if employer.kind not in _ORGANIZATION_KINDS or employer.status != "active":
        raise WorkShiftError("employer_inactive", "The registered employer is unavailable.")
    if workplace.kind not in {"place", "location"} or workplace.status != "active":
        raise WorkShiftError("workplace_inactive", "The registered workplace is unavailable.")
    if actor.location_id != posting.location_id:
        raise WorkShiftError("actor_not_at_workplace", "The worker must remain at the posting workplace through completion.")
    treasury_raw = employer.components.get("treasury")
    if not isinstance(treasury_raw, dict):
        raise WorkShiftError("employer_treasury_missing", "The employer has no registered treasury for this currency.")
    try:
        treasury = TreasuryComponent.model_validate(treasury_raw)
    except Exception as exc:
        raise WorkShiftError("employer_treasury_invalid", "The employer treasury is not valid.") from exc
    employer_account_id = treasury.accounts.get(terms.resource_id)
    if employer_account_id is None:
        raise WorkShiftError("employer_account_missing", "The employer has no registered account for this currency.")
    if worker_account_id == employer_account_id:
        raise WorkShiftError("accounts_not_distinct", "Worker and employer must use distinct holder accounts.")

    worker_account = _holder_account(state, worker_account_id, actor_id, terms.resource_id, "worker")
    employer_account = _holder_account(state, employer_account_id, posting.employer_id, terms.resource_id, "employer")
    if employer_account.balance < terms.wage:
        raise WorkShiftError("insufficient_employer_funds", "The employer does not have enough finite funds for this shift.")

    with localcontext() as context:
        context.prec = 40
        worker_after = worker_account.balance + terms.wage
        employer_after = employer_account.balance - terms.wage
    # Re-run the existing resource bounds on prospective balances. This also
    # rejects an addition that would overflow the account's supported range.
    try:
        from .models import ResourceAccount

        ResourceAccount(
            resource_id=terms.resource_id,
            owner_id=actor_id,
            account_type="holder",
            balance=worker_after,
        )
        ResourceAccount(
            resource_id=terms.resource_id,
            owner_id=posting.employer_id,
            account_type="holder",
            balance=employer_after,
        )
    except Exception as exc:
        raise WorkShiftError("balance_out_of_range", "A resulting work-shift balance exceeds supported resource limits.") from exc
    _check_employer_inventory_floor(employer, employer_account_id, employer_after)

    return PaidWorkShiftPlan(
        terms=terms,
        actor_id=actor_id,
        employer_id=posting.employer_id,
        workplace_id=posting.location_id,
        assignment_id=assignment_id,
        posting_revision=posting.revision,
        assignment_revision=assignment.revision,
        shift_started_tick=shift_started_tick,
        completed_tick=completed_tick,
        ledger_entries=(
            WorkShiftLedgerEntry(
                account_id=worker_account_id,
                owner_id=actor_id,
                resource_id=terms.resource_id,
                amount=terms.wage,
                balance_before=worker_account.balance,
                balance_after=worker_after,
            ),
            WorkShiftLedgerEntry(
                account_id=employer_account_id,
                owner_id=posting.employer_id,
                resource_id=terms.resource_id,
                amount=terms.wage.copy_negate(),
                balance_before=employer_account.balance,
                balance_after=employer_after,
            ),
        ),
    )


def registered_work_shift_terms(state: WorldState) -> dict[UUID, WorkShiftTerms]:
    """Parse immutable authored per-posting shift terms from world metadata.

    The metadata key is an optional list named ``work_shift_terms``. Absence
    means the world has no paid-shift registrations. Every row is closed and
    posting IDs are unique and must bind to an existing, unique posting.
    """

    from .models import WorldState

    if not isinstance(state, WorldState):
        raise WorkShiftError("invalid_world_state", "A validated world state is required.")
    raw_rows = state.metadata.get("work_shift_terms")
    if raw_rows is None:
        return {}
    if not isinstance(raw_rows, list):
        raise WorkShiftError("work_shift_terms_invalid", "Registered work-shift terms must be a list.")

    postings: dict[UUID, int] = {}
    try:
        for raw in state.work_postings:
            posting = WorkPosting.model_validate(raw.model_dump(mode="python"))
            postings[posting.posting_id] = postings.get(posting.posting_id, 0) + 1
    except Exception as exc:
        raise WorkShiftError("work_shift_postings_invalid", "Work postings are invalid.") from exc

    result: dict[UUID, WorkShiftTerms] = {}
    for raw in raw_rows:
        try:
            terms = WorkShiftTerms.model_validate(raw)
        except Exception as exc:
            raise WorkShiftError("work_shift_terms_invalid", "A registered work-shift term is invalid.") from exc
        if terms.posting_id in result:
            raise WorkShiftError("duplicate_work_shift_terms", "A posting may have only one registered shift policy.")
        if postings.get(terms.posting_id) != 1:
            raise WorkShiftError("work_shift_posting_missing", "Registered shift terms must reference one known posting.")
        result[terms.posting_id] = terms
    return result


def revalidate_paid_work_shift(
    state: WorldState,
    plan: PaidWorkShiftPlan,
    *,
    terms: WorkShiftTerms,
    actor_id: UUID,
) -> PaidWorkShiftPlan:
    """Recompute a saved plan against live state before host command creation."""

    plan = PaidWorkShiftPlan.model_validate(plan.model_dump(mode="python"))
    if plan.terms != WorkShiftTerms.model_validate(terms.model_dump(mode="python")):
        raise WorkShiftError("registered_terms_changed", "The posting's registered work-shift terms changed.")
    actor_id = _uuid(actor_id, "actor")
    if actor_id != plan.actor_id:
        raise WorkShiftError("actor_changed", "Work-shift settlement must remain bound to its original worker.")
    refreshed = plan_paid_work_shift(
        state,
        terms=terms,
        actor_id=actor_id,
        assignment_id=plan.assignment_id,
        worker_account_id=plan.ledger_entries[0].account_id,
        shift_started_tick=plan.shift_started_tick,
        expected_posting_revision=plan.posting_revision,
        expected_assignment_revision=plan.assignment_revision,
    )
    if refreshed != plan:
        raise WorkShiftError("stale_work_shift_plan", "Work-shift balances or terms changed before settlement.")
    return refreshed


def _checked_state(state: WorldState) -> WorldState:
    from .models import WorldState

    if not isinstance(state, WorldState):
        raise WorkShiftError("invalid_world_state", "A validated world state is required.")
    try:
        # Re-parse the used envelope to defend against model_copy(update=...).
        return WorldState.model_validate(state.model_dump(mode="python"))
    except Exception as exc:
        raise WorkShiftError("invalid_world_state", "The world state is not valid for work settlement.") from exc


def _one_posting(state: WorldState, posting_id: UUID) -> WorkPosting:
    rows = [row for row in state.work_postings if row.posting_id == posting_id]
    if len(rows) != 1:
        raise WorkShiftError("posting_unavailable", "The registered work posting is unavailable.")
    try:
        return WorkPosting.model_validate(rows[0].model_dump(mode="python"))
    except Exception as exc:
        raise WorkShiftError("posting_invalid", "The registered work posting is invalid.") from exc


def _one_assignment(state: WorldState, assignment_id: UUID) -> WorkAssignment:
    rows = [row for row in state.work_assignments if row.assignment_id == assignment_id]
    if len(rows) != 1:
        raise WorkShiftError("assignment_unavailable", "The active employment assignment is unavailable.")
    try:
        return WorkAssignment.model_validate(rows[0].model_dump(mode="python"))
    except Exception as exc:
        raise WorkShiftError("assignment_invalid", "The employment assignment is invalid.") from exc


def _entity(state: WorldState, entity_id: UUID, role: str) -> WorldEntity:
    from .models import WorldEntity

    raw = state.entities.get(entity_id)
    if raw is None:
        raise WorkShiftError(f"{role}_unavailable", f"The registered {role} is unavailable.")
    try:
        entity = WorldEntity.model_validate(raw.model_dump(mode="python"))
    except Exception as exc:
        raise WorkShiftError(f"{role}_invalid", f"The registered {role} is invalid.") from exc
    if entity.id != entity_id:
        raise WorkShiftError(f"{role}_id_mismatch", f"The registered {role} identifier does not match its record.")
    return entity


def _holder_account(
    state: WorldState, account_id: UUID, owner_id: UUID, resource_id: str, role: str,
) -> ResourceAccount:
    from .models import ResourceAccount

    raw = state.resources.get(account_id)
    if raw is None:
        raise WorkShiftError(f"{role}_account_missing", f"The {role} holder account is unavailable.")
    try:
        account = ResourceAccount.model_validate(raw.model_dump(mode="python"))
    except Exception as exc:
        raise WorkShiftError(f"{role}_account_invalid", f"The {role} holder account is invalid.") from exc
    if (
        account.account_type != "holder"
        or account.owner_id != owner_id
        or account.resource_id != resource_id
    ):
        raise WorkShiftError(
            f"{role}_account_mismatch", f"The {role} account must be an owned holder of the registered currency.",
        )
    return account


def _check_employer_inventory_floor(employer: WorldEntity, account_id: UUID, balance_after: Decimal) -> None:
    raw = employer.components.get("inventory")
    if raw is None:
        return
    try:
        inventory = InventoryComponent.model_validate(raw)
    except Exception as exc:
        raise WorkShiftError("employer_inventory_invalid", "The employer's inventory ledger is invalid.") from exc
    total = Decimal("0")
    with localcontext() as context:
        context.prec = 50
        for lot in inventory.lots:
            if lot.account_id == account_id:
                total += lot.quantity
    if total > balance_after:
        raise WorkShiftError(
            "employer_inventory_balance_conflict",
            "The wage debit would place the employer below its already-recorded inventory lots.",
        )


def _uuid(value: UUID, role: str) -> UUID:
    if not isinstance(value, UUID):
        raise WorkShiftError("invalid_identifier", f"The work-shift {role} identifier must be a UUID.")
    return value


def _tick(value: int, role: str) -> int:
    if type(value) is not int or not _MIN_TICK <= value <= _MAX_TICK:
        raise WorkShiftError("invalid_tick", f"The work-shift {role} tick is invalid.")
    return value


def _revision(value: int, role: str) -> int:
    if type(value) is not int or not 1 <= value <= _MAX_REVISION:
        raise WorkShiftError("invalid_revision", f"The expected {role} revision is invalid.")
    return value
