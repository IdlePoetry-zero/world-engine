"""Read the player's station investigation from committed, branch-local arrival receipts."""

from uuid import UUID

from world_engine.domain.modern_opening_goal import ARRIVAL_PROGRESS, opening_target


def enrich_modern_opening_goal(repository, scope, view, actor_id, state, projected):
    """Recover station visits made by direct movement before durable goal receipts existed."""
    if state.get("metadata", {}).get("pack_id") != "modern_city" or not view.last_seq:
        return
    actor = str(actor_id)
    own_goals = {goal["id"]: goal for goal in projected.get("goals", [])}
    for item in state.get("goals", []):
        public = own_goals.get(item["id"])
        if public is None or item["status"] not in {"active", "paused"}:
            continue
        target = opening_target(state["metadata"], actor, item)
        if target is None:
            continue
        tick = repository.first_actor_direct_arrival(
            scope, view.world.world_id, view.branch.branch_id, actor_id, UUID(target), through_seq=view.last_seq,
        )
        if tick is not None:
            public.update(project_modern_opening_goal(state, actor, item, projected["entities"], tick))
        return


def project_modern_opening_goal(state, actor_id, item, projected_entities, direct_arrival_tick=None):
    metadata = state.get("metadata", {})
    target = opening_target(metadata, actor_id, item)
    if not target or item.get("status") not in {"active", "paused"}:
        return {}
    destination = projected_entities.get(target, {}).get("name") or "交通站点"
    ticks = [
        int(journey["resolved_tick"]) for journey in state.get("travel_journeys", [])
        if journey.get("actor_id") == actor_id and journey.get("to_place_id") == target
        and journey.get("status") == "arrived" and journey.get("resolved_tick") is not None
    ]
    if direct_arrival_tick is not None:
        ticks.append(direct_arrival_tick)
    result = {"title": "调查末班车停运的影响"}
    if not ticks:
        if item.get("progress", 0) >= ARRIVAL_PROGRESS:
            # A state-only caller may lack old direct movements. A numeric progress edit
            # alone never proves that the actor visited a place.
            result["description"] = "可以核实夜班医护和货运人员受停运影响的情况，并把亲历与转述分别记录。"
        else:
            result["description"] = f"前往{destination}核实夜班医护和货运人员受到的影响。也可以先与眼前的人交谈，决定稍后再去。"
        return result
    result["progress"] = max(item.get("progress", 0), ARRIVAL_PROGRESS)
    result["arrival_tick"] = str(min(ticks))
    registration = metadata.get("opening_investigation")
    if isinstance(registration, dict):
        anchors = [(registration.get("anchor_id"), state["entities"].get(registration.get("anchor_id"), {}))]
        opening = anchors[0][1].get("location_id")
    else:
        anchors = state["entities"].items()
        opening = metadata["content_index"]["settlement_ids"][0]
    opening_name = projected_entities.get(opening, {}).get("name")
    clue = "可以在站点询问当时的情况，分别记下亲历与转述。"
    if opening_name:
        # The official opening anchor is public at the starting settlement. Show no remote witness,
        # hidden artifact or another actor's case. The lead is grounded at its original, known location.
        for anchor_id, entity in anchors:
            components = entity.get("components", {})
            case = components.get("investigation_case")
            if not isinstance(case, dict) or not case.get("case_id") or entity.get("location_id") != opening or components.get("hidden"):
                continue
            if isinstance(registration, dict):
                if case["case_id"] != registration.get("case_id"):
                    continue
            elif case.get("title") != "车站停办传言案":
                continue
            own_file = next((record for record in state.get("investigation_cases", [])
                             if record.get("owner_id") == actor_id and record.get("anchor_id") == anchor_id
                             and record.get("case_id") == case["case_id"]), None)
            if own_file is None:
                clue = f"你可以在站内询问停运的影响，也可以到{opening_name}查看车站旧案封套，建立私人案卷。"
            elif own_file["status"] == "closed":
                clue = f"你的「{own_file['title']}」已归档。可查看案卷结论，或继续询问停运给值夜班的人带来了哪些影响。"
            else:
                clue = f"打开调查日志中的「{own_file['title']}」，回到{opening_name}向值守者询问消息来路，再用独立记录核对。"
            break
    result["description"] = f"已抵达{destination}。{clue}"
    return result
