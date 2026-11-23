"""Stable acknowledgments for explicit wait, rest and search jobs."""

from uuid import UUID

from world_engine.domain.models import AccessScope, JobRecord
from world_engine.simulation import LongActionPlan


def stationary_message_key(scope: AccessScope, request_id: UUID) -> str:
    return f"stationary-message:{scope.user_id}:{request_id}"


def stationary_queue_reply(request_id: UUID, job: JobRecord) -> dict:
    plan = LongActionPlan.model_validate(job.payload["plan"])
    if job.job_type != "simulation.long_action" or not (plan.stationary_only or plan.exploration is not None):
        raise ValueError("The job must be an explicit stationary plan.")
    action = {"rest": "休息", "wait": "等待", "explore": "搜查"}[plan.activity]
    content = (
        "搜查已排队。作业会处理搜查期间到期的事件，并在搜查结束时揭示结果。"
        if plan.activity == "explore"
        else f"{action}已排队，将按你指定的时长推进。作业会处理期间到期的事件。"
    )
    return {
        "message_id": str(request_id), "kind": "action", "world_effect": False,
        "content": content,
        "action_status": "succeeded", "job_id": str(job.job_id), "elapsed_ticks": "0", "references": [],
    }
