"""Strict chat intent and stable response helpers for queued study plans."""

from __future__ import annotations

import re
from typing import Annotated, Literal
from uuid import UUID

from pydantic import Field

from world_engine.domain.models import AccessScope, CalendarConfig, ContractModel, JobRecord
from world_engine.simulation import LongActionPlan, StopCondition


MAX_SAFE_INTEGER = 2**53 - 1
MAX_TICK = 2**63 - 1
STUDY_SAFETY_TEXT = re.compile(
    r"安全值\s*(?:达到或低于|低于或等于|小于或等于|不高于|不超过|下降到|降低到|降到|降至|<=|≤)"
    r"\s*([0-9]{1,3})(?![0-9.])"
)


class QueueStudyIntent(ContractModel):
    """An explicit chat request to enqueue a study long action."""

    kind: Literal["queue_study"]
    skill_id: str = Field(min_length=1, max_length=160, pattern=r"^[a-zA-Z][a-zA-Z0-9_.-]*$")
    amount: Annotated[int, Field(strict=True, gt=0, le=MAX_SAFE_INTEGER)]
    unit: Literal["second", "hour", "day", "year"]
    study_stop_safety_at_or_below: Annotated[int, Field(strict=True, ge=0, le=100)] | None = None


def study_safety_stop(actor_id: UUID, threshold: int | None) -> tuple[StopCondition, ...]:
    """Bind an optional study pause to the practising character's safety metric."""

    if threshold is None:
        return ()
    return (StopCondition(kind="need_at_or_below", subject_id=actor_id, need="safety", threshold=threshold),)


def study_message_key(scope: AccessScope, request_id: UUID) -> str:
    """Return the stable, account-scoped idempotency key for a study message."""

    return f"study-message:{scope.user_id}:{request_id}"


def study_duration_seconds(amount: int, unit: str, calendar: CalendarConfig) -> int:
    """Convert an already validated study duration using the branch calendar."""

    if isinstance(amount, bool) or not isinstance(amount, int) or not 1 <= amount <= MAX_SAFE_INTEGER:
        raise ValueError("Study duration must be a positive safe integer.")
    multipliers = {
        "second": 1,
        "hour": 60 * 60,
        "day": calendar.day_seconds,
        "year": calendar.day_seconds * calendar.year_days,
    }
    if unit not in multipliers:
        raise ValueError("Study duration unit is not supported.")
    duration = amount * multipliers[unit]
    if duration > MAX_SAFE_INTEGER:
        raise ValueError("Study duration exceeds the safe integer limit.")
    return duration


def study_queue_reply(request_id: UUID, job: JobRecord) -> dict:
    """Build the immutable chat result from the originally persisted study plan.

    The reply deliberately omits mutable status, progress, and committed tick data,
    so recovering the message after a worker starts cannot change its meaning.
    """

    if job.job_type != "simulation.long_action":
        raise ValueError("The referenced job is not a simulation long action.")
    plan = LongActionPlan.model_validate(job.payload.get("plan"))
    if plan.activity != "study" or plan.skill_id is None or plan.started_tick is None:
        raise ValueError("The referenced long action is not a persisted study plan.")
    duration = plan.target_tick - plan.started_tick
    if not 0 < duration <= MAX_SAFE_INTEGER:
        raise ValueError("The persisted study plan has an invalid duration.")
    safety_pause = next((condition.threshold for condition in plan.stop_conditions
                         if condition.kind == "need_at_or_below" and condition.need == "safety"
                         and condition.subject_id == plan.actor_id), None)
    pause_text = f"角色安全值达到或低于 {safety_pause} 时暂停。" if safety_pause is not None else ""
    return {
        "message_id": str(request_id),
        "kind": "action",
        "content": (
            f"学习作业已排队，计划持续 {duration} 秒，目标世界时刻为 {plan.target_tick}。"
            f"{pause_text}"
            "作业开始后按模拟窗口推进；本次排队没有推进世界时间。"
        ),
        "world_effect": False,
        "action_status": "succeeded",
        "job_id": str(job.job_id),
        "elapsed_ticks": "0",
        "references": [plan.skill_id],
    }

