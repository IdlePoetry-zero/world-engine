"""Stable acknowledgments for explicit wait and rest jobs."""

from uuid import UUID

from world_engine.domain.models import AccessScope, JobRecord
from world_engine.simulation import LongActionPlan


def stationary_message_key(scope: AccessScope, request_id: UUID) -> str:
    return f"stationary-message:{scope.user_id}:{request_id}"


def stationary_queue_reply(request_id: UUID, job: JobRecord) -> dict:
    plan = LongActionPlan.model_validate(job.payload["plan"])
    if job.job_type != "simulation.long_action" or not plan.stationary_only:
        raise ValueError("The job must be an explicit stationary plan.")
    action = "休息" if plan.activity == "rest" else "等待"
    return {
        "message_id": str(request_id), "kind": "action", "world_effect": False,
        "content": f"{action}已排队，将按你指定的时长推进。作业会处理期间到期的事件。",
        "action_status": "succeeded", "job_id": str(job.job_id), "elapsed_ticks": "0", "references": [],
    }
