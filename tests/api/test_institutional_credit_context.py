from __future__ import annotations

import json
from decimal import Decimal
from uuid import UUID, uuid4

import pytest

from world_engine.application.institutional_credit_context import MAX_CONTEXT_ROWS, institutional_credit_context
from world_engine.domain.components import GovernanceComponent, OfficeRecord, TreasuryComponent
from world_engine.domain.institutional_credit import (
    accept_institutional_credit,
    mark_institutional_credit_overdue,
    pay_institutional_credit,
    propose_institutional_credit,
)
from world_engine.domain.models import ResourceAccount, WorldEntity, WorldState


def _organization(
    *,
    name: str,
    actor_id=None,
    currencies: tuple[str, ...] = (),
    resources: dict | None = None,
    hidden: bool = False,
):
    organization_id = uuid4()
    components = {}
    if actor_id is not None:
        components["governance"] = GovernanceComponent(
            members=[actor_id],
            offices=[OfficeRecord(
                office_id="office.treasurer",
                title="Treasurer",
                holder_id=actor_id,
                authority=["authority.treasury"],
            )],
        ).model_dump(mode="json")
    accounts = {}
    for currency in currencies:
        account_id = uuid4()
        accounts[currency] = account_id
        if resources is not None:
            resources[account_id] = ResourceAccount(
                resource_id=currency,
                balance=Decimal("123456789.25"),
                owner_id=organization_id,
                account_type="holder",
            )
    if currencies:
        components["treasury"] = TreasuryComponent(accounts=accounts).model_dump(mode="json")
    if hidden:
        components["hidden"] = True
    entity = WorldEntity(
        id=organization_id,
        kind="company",
        name=name,
        components=components,
    )
    return entity


def _signer(name: str = "Signer") -> WorldEntity:
    return WorldEntity(id=uuid4(), kind="character", name=name)


def _offer(lender_id, borrower_id, *, tick: int = 9, credit_id=None):
    return propose_institutional_credit(
        credit_id=credit_id or uuid4(),
        lender_organization_id=lender_id,
        borrower_organization_id=borrower_id,
        currency_resource_id="custom.credit",
        principal_minor=900719925474099312345678,
        total_repayable_minor=900719925474099312345999,
        accepted_through_tick=tick + 2,
        maturity_tick=tick + 12,
        current_tick=tick,
    )


@pytest.mark.asyncio
async def test_scene_credit_preview_rechecks_projection_and_confirmation(tmp_path):
    from world_engine.api.contracts import MessageRequest
    from world_engine.application.actions import RulesActionDispatcher
    from world_engine.application.conversation import ConversationApplication, ConversationStore
    from world_engine.application.operations import OperationsStore
    from world_engine.application.worlds import WorldApplication
    from world_engine.domain.models import AccessScope, CommandEnvelope, NewWorld
    from world_engine.rules.contracts import ACTION_INTENT_ADAPTER, OfferInstitutionalCreditIntent, RuleContext
    from world_engine.rules.engine import RuleEngine
    from world_engine.storage.database import Database
    from world_engine.storage.repository import WorldRepository

    class NoModel:
        async def generate(self, *_args):
            raise AssertionError("A typed button and confirmation require no action-interpretation model.")

    access = AccessScope(tenant_id=uuid4(), user_id=uuid4())
    protagonist = _signer("Treasurer")
    resources = {}
    borrower = _organization(name="Borrower org", actor_id=protagonist.id, currencies=("custom.credit",), resources=resources)
    lender = _organization(name="Lender org", actor_id=protagonist.id, currencies=("custom.credit",), resources=resources)
    draft = NewWorld(title="Credit decision", state=WorldState(
        player_id=protagonist.id, current_tick=9,
        entities={entity.id: entity for entity in (protagonist, borrower, lender)},
        resources=resources,
    ))
    database = Database(f"sqlite:///{(tmp_path / 'credit.db').as_posix()}")
    try:
        database.create_schema()
        repository = WorldRepository(database)
        repository.create_world(access, draft)
        initial = repository.read_world(access, draft.world_id, draft.branch_id)
        models = NoModel()
        conversation = ConversationApplication(
            WorldApplication(repository, None), models, ConversationStore(OperationsStore(tmp_path / "operations.db")),
        )
        conversation.actions = RulesActionDispatcher(repository, models, RuleEngine(), ACTION_INTENT_ADAPTER, RuleContext)
        message = {
            "world_id": draft.world_id, "branch_id": draft.branch_id, "actor_id": protagonist.id,
            "session_id": uuid4(), "expected_state_version": str(initial.branch.state_version), "mode": "action",
        }
        offer_intent = OfferInstitutionalCreditIntent(
            lender_organization_id=lender.id, borrower_organization_id=borrower.id, currency_resource_id="custom.credit",
            principal_minor=5, accepted_through_tick=11, maturity_tick=21,
        )
        command_id = uuid4()
        offered = RuleEngine().evaluate(initial.state, offer_intent, RuleContext(
            world_id=draft.world_id, branch_id=draft.branch_id, actor_id=protagonist.id,
            command_id=command_id, ruleset_version=initial.world.ruleset_version,
        ))
        assert offered.outcome.status == "succeeded", offered.outcome
        repository.commit_command(access, draft.world_id, draft.branch_id, CommandEnvelope(
            command_id=command_id, actor_id=protagonist.id, idempotency_key="offer-before-panel",
            expected_state_version=initial.branch.state_version, command_type="action.offer_institutional_credit",
            payload=offer_intent.model_dump(mode="json"),
        ), offered.events)
        before = repository.read_world(access, draft.world_id, draft.branch_id)
        offer, = before.state.institutional_credit_contracts
        message |= {"expected_state_version": str(before.branch.state_version),
                    "text": "核对拒绝借款。", "draft": {"operation": "prepare"}}
        choice = {"kind": "resolve_institutional_credit", "credit_id": offer.credit_id, "operation": "decline",
                  "expected_revision": "1"}
        invalid = await conversation.send(access, MessageRequest(
            **message, request_id=uuid4(), scene_action=choice | {"operation": "pay", "amount_minor": "1"},
        ))
        assert "action_draft" not in invalid and invalid["world_effect"] is False
        preview = await conversation.send(access, MessageRequest(**message, request_id=uuid4(), scene_action=choice))
        assert preview["world_effect"] is False and preview["action_draft"]["can_confirm"]
        assert repository.read_world(access, draft.world_id, draft.branch_id).state_hash == before.state_hash
        readback = preview["action_draft"]
        confirmation = message | {"draft": {"operation": "confirm", "draft_id": readback["draft_id"], "revision": readback["revision"]}}
        submitted = MessageRequest(**confirmation, request_id=uuid4())
        committed = await conversation.send(access, submitted)
        assert committed["world_effect"] is True
        final = repository.read_world(access, draft.world_id, draft.branch_id)
        assert final.state.institutional_credit_contracts[0].status == "declined"
        assert final.state.current_tick == before.state.current_tick
        assert final.state.resources == before.state.resources
        assert await conversation.send(access, submitted) == committed
        assert repository.replay(access, draft.world_id, draft.branch_id, from_genesis=True).state_hash == final.state_hash
    finally:
        database.dispose()


def test_context_uses_direct_signing_authority_and_only_visible_shared_treasury_units():
    resources = {}
    own_signer = _signer()
    own = _organization(
        name="Own Company",
        actor_id=own_signer.id,
        currencies=("custom.credit", "custom.reserve"),
        resources=resources,
    )
    shared = _organization(
        name="Visible Counterparty",
        currencies=("custom.credit",),
        resources=resources,
    )
    no_shared_unit = _organization(
        name="Unmatched Counterparty",
        currencies=("custom.other",),
        resources=resources,
    )
    hidden = _organization(
        name="HIDDEN_ORGANIZATION_NAME",
        currencies=("custom.credit",),
        resources=resources,
        hidden=True,
    )
    outsider_a = _organization(name="Unrelated A")
    outsider_b = _organization(name="Unrelated B")
    visible_offer = _offer(own.id, hidden.id)
    unrelated_offer = _offer(outsider_a.id, outsider_b.id, credit_id=uuid4())
    state = WorldState(
        current_tick=9,
        entities={
            entity.id: entity
            for entity in (own_signer, own, shared, no_shared_unit, hidden, outsider_a, outsider_b)
        },
        resources=resources,
        institutional_credit_contracts=[visible_offer, unrelated_offer],
    )

    context = institutional_credit_context(
        state,
        own_signer.id,
        visible_organization_ids={own.id, shared.id, no_shared_unit.id, hidden.id},
    )

    assert context["represented_institutions"] == [{
        "organization_id": str(own.id),
        "name": "Own Company",
        "currency_resource_ids": ["custom.credit", "custom.reserve"],
        "currency_resource_ids_truncated": False,
    }]
    assert context["visible_credit_counterparties"] == [{
        "represented_organization_id": str(own.id),
        "counterparty_organization_id": str(shared.id),
        "counterparty_name": "Visible Counterparty",
        "shared_currency_resource_ids": ["custom.credit"],
        "shared_currency_resource_ids_truncated": False,
    }]
    assert len(context["institutional_credit_contracts"]) == 1
    contract = context["institutional_credit_contracts"][0]
    assert contract["credit_id"] == str(visible_offer.credit_id)
    assert contract["status"] == "offered"
    assert contract["phase"] == "offer_pending"
    assert contract["principal_minor"] == "900719925474099312345678"
    assert contract["total_due_minor"] == "900719925474099312345999"
    assert contract["remaining_total_minor"] == "900719925474099312345999"
    assert contract["accepted_through_tick"] == 11
    assert contract["maturity_tick"] == 21
    assert contract["can_withdraw"] is True
    assert contract["can_accept"] is False
    assert contract["can_decline"] is False
    assert contract["can_pay"] is False

    encoded = json.dumps(context, sort_keys=True)
    assert "HIDDEN_ORGANIZATION_NAME" not in encoded
    assert str(shared.components["treasury"]["accounts"]["custom.credit"]) not in encoded
    assert "123456789.25" not in encoded
    assert "balance" not in encoded
    assert "Unrelated A" not in encoded
    assert "Unrelated B" not in encoded


def test_contract_actions_follow_party_authority_phase_and_inclusive_deadlines():
    resources = {}
    lender_signer = _signer("Lender signer")
    borrower_signer = _signer("Borrower signer")
    lender = _organization(
        name="Lender", actor_id=lender_signer.id,
        currencies=("custom.credit",), resources=resources,
    )
    borrower = _organization(
        name="Borrower", actor_id=borrower_signer.id,
        currencies=("custom.credit",), resources=resources,
    )
    offered = _offer(lender.id, borrower.id, tick=9)
    state = WorldState(
        current_tick=11,
        entities={entity.id: entity for entity in (lender_signer, borrower_signer, lender, borrower)},
        resources=resources,
        institutional_credit_contracts=[offered],
    )

    borrower_view = institutional_credit_context(
        state, borrower_signer.id, visible_organization_ids={lender.id, borrower.id},
    )
    offer_row = borrower_view["institutional_credit_contracts"][0]
    assert offer_row["can_accept"] is True  # accepted_through_tick is inclusive
    assert offer_row["can_decline"] is True
    assert offer_row["can_withdraw"] is False

    accepted = accept_institutional_credit(
        offered,
        actor_organization_id=borrower.id,
        current_tick=11,
        expected_revision=1,
    )
    active_state = state.model_copy(update={"institutional_credit_contracts": [accepted]})
    active_row = institutional_credit_context(
        active_state,
        borrower_signer.id,
        visible_organization_ids={lender.id, borrower.id},
    )["institutional_credit_contracts"][0]
    assert active_row["status"] == "active"
    assert active_row["can_accept"] is False
    assert active_row["can_pay"] is True

    overdue = mark_institutional_credit_overdue(
        accepted,
        actor_organization_id=lender.id,
        current_tick=22,
        expected_revision=2,
    )
    overdue_state = active_state.model_copy(update={
        "current_tick": 22,
        "institutional_credit_contracts": [overdue],
    })
    overdue_row = institutional_credit_context(
        overdue_state,
        borrower_signer.id,
        visible_organization_ids={lender.id, borrower.id},
    )["institutional_credit_contracts"][0]
    assert overdue_row["status"] == "overdue"
    assert overdue_row["overdue_at_tick"] == 22
    assert overdue_row["can_pay"] is True

    settled = pay_institutional_credit(
        overdue,
        actor_organization_id=borrower.id,
        amount_minor=overdue.remaining_total_minor,
        current_tick=22,
        expected_revision=3,
    )
    settled_state = overdue_state.model_copy(update={"institutional_credit_contracts": [settled]})
    settled_row = institutional_credit_context(
        settled_state,
        borrower_signer.id,
        visible_organization_ids={lender.id, borrower.id},
    )["institutional_credit_contracts"][0]
    assert settled_row["status"] == "settled"
    assert settled_row["can_pay"] is False


def test_contract_context_prioritizes_open_offers_when_older_records_fill_the_limit():
    signer = _signer()
    resources = {}
    borrower = _organization(
        name="Borrower", actor_id=signer.id, currencies=("custom.credit",), resources=resources,
    )
    lender = _organization(name="Lender", currencies=("custom.credit",), resources=resources)
    expired = [_offer(lender.id, borrower.id, tick=9, credit_id=UUID(int=index + 1))
               for index in range(MAX_CONTEXT_ROWS)]
    live_offer = _offer(lender.id, borrower.id, tick=20, credit_id=UUID(int=10_000))
    state = WorldState(
        current_tick=20,
        entities={entity.id: entity for entity in (signer, borrower, lender)},
        resources=resources,
        institutional_credit_contracts=[*expired, live_offer],
    )

    context = institutional_credit_context(state, signer.id, visible_organization_ids={lender.id})

    assert context["institutional_credit_contracts_truncated"] is True
    active, *history = context["institutional_credit_contracts"]
    assert active["credit_id"] == str(live_offer.credit_id)
    assert active["can_accept"] is True
    assert all(item["phase"] == "offer_expired" and not item["can_accept"] for item in history)


def test_absent_or_unauthorized_actor_gets_no_counterparty_or_debt_scan_results():
    resources = {}
    signer = _signer()
    own = _organization(
        name="Represented", actor_id=signer.id,
        currencies=("custom.credit",), resources=resources,
    )
    outsider = _organization(name="Outsider")
    debt = _offer(own.id, outsider.id)
    ordinary_member = _signer("Ordinary member")
    # Include the actor in membership without granting an office.
    governance = GovernanceComponent(members=[ordinary_member.id]).model_dump(mode="json")
    own_without_signing = own.model_copy(update={
        "components": {**own.components, "governance": governance},
    })
    state = WorldState(
        current_tick=9,
        entities={
            ordinary_member.id: ordinary_member,
            own_without_signing.id: own_without_signing,
            outsider.id: outsider,
        },
        resources=resources,
        institutional_credit_contracts=[debt],
    )

    context = institutional_credit_context(
        state,
        ordinary_member.id,
        visible_organization_ids={outsider.id},
    )

    assert context["represented_institutions"] == []
    assert context["visible_credit_counterparties"] == []
    assert context["institutional_credit_contracts"] == []

