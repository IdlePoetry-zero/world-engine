"""Private model dispatch for charter-authorized vacancy ballot votes."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from uuid import UUID

from world_engine.domain.models import AccessScope, JobLease, JobRecord
from world_engine.simulation.contracts import DecisionRequest, DecisionResponse

from .leadership_ballot_decisions import choose_leadership_vote
from .model_decisions import InvalidModelDecisionOutput, MODEL_DECISION_TIMEOUT_SECONDS


async def invoke_autonomous_vacancy_vote(
    operations,
    repository,
    scope: AccessScope,
    lease: JobLease,
    job: JobRecord,
    decision: DecisionRequest,
    *,
    timeout_seconds: float = 100,
) -> DecisionResponse:
    """Ask one authorized NPC to privately vote on its tracked vacancy ballot.

    The returned response is tied to the exact branch state version and tick
    named by the request. This function never applies a vote; simulation
    planning validates and commits the response through its ordinary path.
    """
    from world_engine.simulation.engine import StaleDecisionResponse

    if type(timeout_seconds) not in {int, float} or not 0 < timeout_seconds <= MODEL_DECISION_TIMEOUT_SECONDS:
        raise ValueError(f"timeout_seconds must be in (0, {MODEL_DECISION_TIMEOUT_SECONDS}].")
    if (
        decision.kind != "organization.vacancy_vote"
        or decision.decision_owner != "model"
        or decision.actor_id is None
    ):
        raise InvalidModelDecisionOutput("The request is not a model-owned autonomous vacancy vote.")
    if (
        lease.job_id != job.job_id
        or lease.world_id != job.world_id
        or lease.branch_id != job.branch_id
    ):
        raise InvalidModelDecisionOutput("The job lease does not match the supplied job.")
    if len(decision.evidence_ids) != 2:
        raise InvalidModelDecisionOutput("A vacancy vote requires organization and ballot evidence IDs.")
    organization_id, ballot_id = decision.evidence_ids
    if tuple(option.option_id for option in decision.options) != ("yes", "no", "abstain"):
        raise InvalidModelDecisionOutput("The vacancy vote request does not have the required bounded choices.")

    view = repository.read_world(scope, lease.world_id, lease.branch_id)
    if (
        view.branch.state_version != decision.state_version
        or view.branch.current_tick != decision.at_tick
        or view.state.current_tick != decision.at_tick
    ):
        raise StaleDecisionResponse("Autonomous vacancy vote no longer matches its branch snapshot.")

    review = view.state.leadership_vacancy_reviews.get(organization_id)
    if review is None or review.ballot_id != ballot_id:
        raise InvalidModelDecisionOutput("The ballot is not tracked by the current vacancy review record.")
    ballots = [
        ballot for ballot in view.state.leadership_ballots
        if ballot.ballot_id == ballot_id and ballot.organization_id == organization_id
    ]
    if len(ballots) != 1:
        raise InvalidModelDecisionOutput("The tracked vacancy ballot is unavailable.")
    ballot = ballots[0]
    voter_id = decision.actor_id
    actor = view.state.entities.get(voter_id)
    if (
        ballot.incumbent_id is not None
        or ballot.vacancy_proposer_id is None
        or ballot.status != "open"
        or actor is None
        or actor.kind not in {"character", "person"}
        or actor.status != "active"
        or voter_id == view.state.player_id
        or voter_id not in ballot.eligible_voter_ids
    ):
        raise InvalidModelDecisionOutput("The requested actor is not an eligible autonomous vacancy voter.")

    # This verifies the world owner may solicit the response and that the actor
    # is not under a user control record in the repository.
    repository.authorize_hosted_npc(scope, lease.world_id, voter_id)

    request = SimpleNamespace(
        request_id=decision.request_id,
        world_id=lease.world_id,
        branch_id=lease.branch_id,
        job_id=lease.job_id,
        actor_id=voter_id,
        expected_state_version=decision.state_version,
        profile=SimpleNamespace(
            speed=job.payload.get("speed", "balanced"),
            depth=job.payload.get("depth", "deep"),
        ),
    )
    choice = await asyncio.wait_for(
        choose_leadership_vote(
            operations, scope, request, view.state, ballot, voter_id, allow_defer=False,
        ),
        timeout=timeout_seconds,
    )
    return DecisionResponse(
        request_id=decision.request_id,
        state_version=decision.state_version,
        at_tick=decision.at_tick,
        source="model",
        option_id=choice,
    )
