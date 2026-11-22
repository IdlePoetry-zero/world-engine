from uuid import UUID

import pytest
from pydantic import ValidationError

from world_engine.domain.family_events import (
    KinshipLinkEndedEvent,
    KinshipLinkRecordedEvent,
    kinship_event_id,
)
from world_engine.domain.family_kinship import EventKinshipSource, KinshipLink


WORLD_ID = UUID(int=100)
BRANCH_ID = UUID(int=101)
COMMAND_ID = UUID(int=102)
ACTOR_ID = UUID(int=103)
PARENT_ID = UUID(int=104)
CHILD_ID = UUID(int=105)
LINK_ID = UUID(int=106)
RULESET = "rules.family-kinship.test.v1"


def _link(*, event_id=None, created_tick=50, ended_tick=None, source_kind="event") -> KinshipLink:
    if source_kind == "event":
        source = EventKinshipSource(kind="event", event_id=event_id or kinship_event_id(
            COMMAND_ID, RULESET, "link_recorded",
        ))
    else:
        source = {"kind": source_kind, "reference_id": "author.source.1"}
    return KinshipLink(
        link_id=LINK_ID,
        parent_id=PARENT_ID,
        child_id=CHILD_ID,
        relationship="birth",
        created_tick=created_tick,
        source=source,
        ended_tick=ended_tick,
    )


def _recorded(**updates) -> KinshipLinkRecordedEvent:
    values = {
        "event_id": kinship_event_id(COMMAND_ID, RULESET, "link_recorded"),
        "world_id": WORLD_ID,
        "branch_id": BRANCH_ID,
        "command_id": COMMAND_ID,
        "ruleset_version": RULESET,
        "actor_id": ACTOR_ID,
        "effective_tick": 50,
        "link": _link(),
    }
    values.update(updates)
    return KinshipLinkRecordedEvent.model_validate(values)


def _ended(**updates) -> KinshipLinkEndedEvent:
    values = {
        "event_id": kinship_event_id(COMMAND_ID, RULESET, "link_ended"),
        "world_id": WORLD_ID,
        "branch_id": BRANCH_ID,
        "command_id": COMMAND_ID,
        "ruleset_version": RULESET,
        "actor_id": ACTOR_ID,
        "effective_tick": 90,
        "link_id": LINK_ID,
        "expected_created_tick": 50,
        "expected_relationship": "birth",
    }
    values.update(updates)
    return KinshipLinkEndedEvent.model_validate(values)


def test_record_event_binds_its_source_tick_and_private_visibility():
    event = _recorded()

    assert event.type == "core.kinship_link_recorded.v1"
    assert event.event_id == kinship_event_id(COMMAND_ID, RULESET, "link_recorded")
    assert event.link.source.event_id == event.event_id
    assert event.link.created_tick == event.effective_tick == 50
    assert event.link.ended_tick is None
    assert event.visibility == {"policy": "author_only"}

    assert kinship_event_id(COMMAND_ID, RULESET, "link_recorded") == kinship_event_id(
        COMMAND_ID, RULESET, "link_recorded",
    )
    assert kinship_event_id(COMMAND_ID, RULESET, "link_recorded") != kinship_event_id(
        UUID(int=999), RULESET, "link_recorded",
    )
    assert kinship_event_id(COMMAND_ID, RULESET, "link_recorded") != kinship_event_id(
        COMMAND_ID, "rules.family-kinship.test.v2", "link_recorded",
    )


@pytest.mark.parametrize(
    "updates",
    [
        {"event_id": UUID(int=200)},
        {"ruleset_version": "rules.other"},
        {"link": _link(event_id=UUID(int=201))},
        {"link": _link(created_tick=51)},
        {"link": _link(ended_tick=80)},
        {"visibility": {"policy": "subject_ids", "subject_ids": [str(ACTOR_ID)]}},
        {"link": {
            "link_id": LINK_ID, "parent_id": PARENT_ID, "child_id": CHILD_ID,
            "relationship": "birth", "created_tick": 50,
            "source": {"kind": "testimony", "reference_id": "unsupported"},
        }},
    ],
    ids=["event-id", "ruleset", "source-id", "tick", "already-ended", "visibility", "untrusted-source"],
)
def test_record_event_rejects_unbound_context_or_link(updates):
    with pytest.raises(ValidationError):
        _recorded(**updates)


def test_end_event_has_exact_event_identity_and_created_tick_compare_and_swap():
    event = _ended()

    assert event.type == "core.kinship_link_ended.v1"
    assert event.event_id == kinship_event_id(COMMAND_ID, RULESET, "link_ended")
    assert event.link_id == LINK_ID
    assert event.expected_created_tick == 50
    assert event.expected_relationship == "birth"
    assert event.effective_tick == 90
    assert event.visibility == {"policy": "author_only"}


@pytest.mark.parametrize(
    "updates",
    [
        {"event_id": UUID(int=300)},
        {"ruleset_version": "rules.other"},
        {"effective_tick": 49},
        {"visibility": {"policy": "public"}},
    ],
    ids=["event-id", "ruleset", "end-before-create", "visibility"],
)
def test_end_event_rejects_invalid_binding_or_interval(updates):
    with pytest.raises(ValidationError):
        _ended(**updates)
