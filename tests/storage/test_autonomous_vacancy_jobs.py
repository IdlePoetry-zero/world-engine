from uuid import UUID, uuid4

import pytest

from world_engine.application.backups import BackupApplication
from world_engine.application.simulation_jobs import SimulationJobs
from world_engine.domain.autonomous_vacancy import AUTONOMOUS_VACANCY_BALLOT_AUTHORITY
from world_engine.domain.components import GovernanceComponent, OfficeRecord, PlaceComponent, PlaceRoute
from world_engine.domain.models import AccessScope, JobStatus, NewWorld, WorldEntity, WorldState
from world_engine.domain.travel_itinerary import plan_travel_itinerary
from world_engine.simulation.contracts import DecisionResponse, LongActionPlan, WorldAdvancePlan
from world_engine.storage.backup_import import WorldBackupImporter
from world_engine.storage.database import Database
from world_engine.storage.repository import WorldRepository


def _make_world():
    player, nominee, other, third = (UUID(int=value) for value in range(1, 5))
    eligible, empty_council, player_election = (UUID(int=value) for value in range(10, 13))
    entities = {
        person: WorldEntity(id=person, kind="character", name=name)
        for person, name in zip((player, nominee, other, third), ("Traveler", "Caretaker", "Surveyor", "Archivist"))
    }
    for organization, policy, members in (
        (eligible, "election", [nominee, other, third]),
        (empty_council, "council", [nominee, other, third]),
        (player_election, "election", [player, nominee, other]),
    ):
        governance = GovernanceComponent(
            members=members, succession_policy=policy, authority_tags=[AUTONOMOUS_VACANCY_BALLOT_AUTHORITY],
            offices=[OfficeRecord(office_id="office.leader", title="Caretaker", holder_id=nominee)],
        )
        entities[organization] = WorldEntity(
            id=organization, kind="organization", name=f"Council {organization.int}",
            components={"governance": governance.model_dump(mode="json")},
        )
    return WorldState(player_id=player, calendar={"day_seconds": 10, "year_days": 10}, entities=entities), eligible, nominee


@pytest.mark.parametrize(("as_character", "first_year_deferred", "target_tick"), [
    (False, False, 210), (False, True, 210), (True, False, 100), (True, True, 210),
])
def test_simulation_reviews_and_elects_only_with_authorized_separate_votes(
    tmp_path, as_character, first_year_deferred, target_tick,
):
    database = Database(f"sqlite:///{(tmp_path / 'vacancy.sqlite').as_posix()}")
    database.create_schema()
    repository = WorldRepository(database)
    scope = AccessScope(tenant_id=uuid4(), user_id=uuid4())
    state, eligible, nominee = _make_world()
    view = repository.create_world(scope, NewWorld(title="Election after a vacancy", state=state))
    world, branch = view.world.world_id, view.branch.branch_id
    service = SimulationJobs(repository)
    plan = (LongActionPlan(actor_id=state.player_id, activity="wait", started_tick=0, target_tick=target_tick)
            if as_character else WorldAdvancePlan(started_tick=0, target_tick=target_tick))
    job = service.enqueue(scope, world, branch, plan,
                          idempotency_key="annual-vacancy", speed="balanced", depth="deep")
    lease = repository.claim_job("annual-vacancy", job_type=job.job_type)
    assert lease is not None and job.job_id == lease.job_id
    requests = []
    try:
        for _ in range(16):
            resolution = service.advance(scope, lease)
            if resolution.outcome == "decision_required":
                (request,) = resolution.decision_requests
                requests.append(request)
                if request.kind == "organization.vacancy_nomination":
                    option = ("defer" if first_year_deferred and request.at_tick == 100 else
                              f"nominate_{nominee.hex}")
                else:
                    assert request.kind == "organization.vacancy_vote"
                    option = "yes"
                service.advance(scope, lease, model_decision=DecisionResponse(
                    request_id=request.request_id, state_version=request.state_version, at_tick=request.at_tick,
                    source="model", option_id=option,
                ))
            if repository.read_job(scope, job.job_id).status is JobStatus.SUCCEEDED:
                break
        assert repository.read_job(scope, job.job_id).status is JobStatus.SUCCEEDED
        current = repository.read_world(scope, world, branch)
        assert current.state.current_tick == target_tick
        assert set(current.state.leadership_vacancy_reviews) == {eligible}
        review = current.state.leadership_vacancy_reviews[eligible]
        assert review.period_index == (2 if first_year_deferred else 1)
        assert [request.at_tick for request in requests if request.kind == "organization.vacancy_nomination"] == (
            [100, 200] if first_year_deferred else [100]
        )
        votes = [request for request in requests if request.kind == "organization.vacancy_vote"]
        assert len(votes) == 2 and votes[0].actor_id != votes[1].actor_id
        (ballot,) = current.state.leadership_ballots
        assert ballot.status == "approved" and ballot.nominee_id == nominee and ballot.ballot_id == review.ballot_id
        governance = GovernanceComponent.model_validate(current.state.entities[eligible].components["governance"])
        assert governance.leader_id == nominee and governance.offices[0].revision == 1
        assert repository.replay(scope, world, branch, from_genesis=True).state_hash == current.state_hash
        backup_app = BackupApplication(repository)
        for version in ("1.0", "2.0"):
            restored_database = Database(f"sqlite:///{(tmp_path / f'vacancy-restored-{version}.sqlite').as_posix()}")
            restored_database.create_schema()
            restored = WorldRepository(restored_database)
            try:
                archive = backup_app.export_world(scope, world, format_version=version)
                with backup_app.validate_archive(archive) as verified:
                    WorldBackupImporter(restored).import_verified_backup(scope, verified)
                assert restored.replay(scope, world, branch, from_genesis=True).state_hash == current.state_hash
            finally:
                restored_database.dispose()
    finally:
        database.dispose()


def test_itinerary_commits_final_arrival_before_same_tick_vacancy_review(tmp_path):
    database = Database(f"sqlite:///{(tmp_path / 'vacancy-itinerary.sqlite').as_posix()}")
    database.create_schema()
    repository = WorldRepository(database)
    scope = AccessScope(tenant_id=uuid4(), user_id=uuid4())
    state, organization, nominee = _make_world()
    player = state.entities[state.player_id]
    origin, destination, route_id = (uuid4() for _ in range(3))
    route = PlaceRoute(route_id=route_id, from_place_id=origin, to_place_id=destination, travel_ticks=100)
    state = WorldState.model_validate(state.model_dump(mode="python") | {"entities": {
        **state.entities,
        player.id: player.model_copy(update={"location_id": origin, "components": {
            "capabilities": {"grants": ["world.travel"]}, "known_places": [str(origin), str(destination)],
        }}),
        origin: WorldEntity(id=origin, kind="place", name="Outpost", components={
            "place": PlaceComponent(routes=[route]).model_dump(mode="json"),
        }),
        destination: WorldEntity(id=destination, kind="place", name="Town"),
    }})
    created = repository.create_world(scope, NewWorld(title="Arrival at the New Year", state=state))
    world, branch = created.world.world_id, created.branch.branch_id
    itinerary = plan_travel_itinerary(state, itinerary_id=uuid4(), actor_id=player.id, route_ids=[route_id], deadline_tick=100)
    plan = LongActionPlan(actor_id=player.id, activity="travel", started_tick=0, target_tick=100, travel_itinerary=itinerary)
    service = SimulationJobs(repository)
    job = service.enqueue(scope, world, branch, plan, idempotency_key="arrival-at-new-year", speed="balanced", depth="deep")
    lease = repository.claim_job("new-year-traveler", job_type=job.job_type)
    assert lease is not None
    nomination_tick = None
    try:
        for _ in range(16):
            step = service.advance(scope, lease)
            if getattr(step, "outcome", None) == "decision_required":
                (request,) = step.decision_requests
                before = repository.read_job(scope, job.job_id)
                assert before.committed_through_tick == 100 and before.payload["itinerary_cursor"]["leg_index"] == 0
                assert repository.read_world(scope, world, branch).state.entities[player.id].location_id == destination
                choice = "yes"
                if request.kind == "organization.vacancy_nomination":
                    nomination_tick = request.at_tick
                    choice = f"nominate_{nominee.hex}"
                service.advance(scope, lease, model_decision=DecisionResponse(
                    request_id=request.request_id, state_version=request.state_version, at_tick=request.at_tick,
                    source="model", option_id=choice,
                ))
            if repository.read_job(scope, job.job_id).status is JobStatus.SUCCEEDED:
                break
        saved = repository.read_job(scope, job.job_id)
        assert saved.status is JobStatus.SUCCEEDED and saved.result["reason_code"] == "travel_itinerary_completed"
        assert nomination_tick == saved.committed_through_tick == 100
        head = repository.read_world(scope, world, branch)
        assert head.state.leadership_vacancy_reviews[organization].period_index == 1
        (journey,) = head.state.travel_journeys
        assert journey.status == "arrived" and journey.resolved_tick == 100
        assert repository.replay(scope, world, branch, from_genesis=True).state_hash == head.state_hash
    finally:
        database.dispose()
