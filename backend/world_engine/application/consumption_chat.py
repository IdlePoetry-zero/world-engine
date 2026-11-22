"""Durable chat acknowledgments for explicitly requested consumption."""

from uuid import UUID

from world_engine.domain.models import AccessScope, JobRecord
from world_engine.simulation import LongActionPlan


def consumption_message_key(scope: AccessScope, request_id: UUID) -> str:
    return f"consume-message:{scope.user_id}:{request_id}"


def consumption_queue_reply(request_id: UUID, job: JobRecord) -> dict:
    if job.job_type != "simulation.long_action":
        raise ValueError("The referenced job is not a simulation long action.")
    plan = LongActionPlan.model_validate(job.payload.get("plan"))
    intent = plan.consumption
    if intent is None:
        raise ValueError("The referenced job is not an exact consumption plan.")
    return {
        "message_id": str(request_id),
        "kind": "action",
        "content": (
            f"补给作业已排队，申请消耗 {intent.quantity} 单位 {intent.resource_id}。"
            "作业先处理当前时刻的待结算事项，再核对补给条件。排队时尚未使用物资。"
        ),
        "world_effect": False,
        "action_status": "succeeded",
        "job_id": str(job.job_id),
        "elapsed_ticks": "0",
        "references": [],
    }
