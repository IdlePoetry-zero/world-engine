from __future__ import annotations

from uuid import uuid4

import pytest

from world_engine.application.institutional_credit_chat import (
    credit_action_matches_context,
    credit_offer_terms_match,
    credit_resolution_matches,
    reserve_mobilization_matches_context,
)
from world_engine.rules.contracts import (
    MobilizeTreasuryReserveIntent,
    OfferInstitutionalCreditIntent,
    ResolveInstitutionalCreditIntent,
)


CURRENCY_ID = "modern.credit"


def _offer(**overrides) -> OfferInstitutionalCreditIntent:
    values = {
        "lender_organization_id": uuid4(),
        "borrower_organization_id": uuid4(),
        "currency_resource_id": CURRENCY_ID,
        "principal_minor": 100,
        "total_repayable_minor": 110,
        "accepted_through_tick": 20,
        "maturity_tick": 30,
    }
    values.update(overrides)
    return OfferInstitutionalCreditIntent(**values)


def _resolution(operation: str, amount_minor: int | None = None) -> ResolveInstitutionalCreditIntent:
    return ResolveInstitutionalCreditIntent(
        credit_id=uuid4(),
        operation=operation,
        expected_revision=1,
        amount_minor=amount_minor,
    )


def test_credit_offer_binds_all_explicit_fixed_terms_and_shared_currency():
    intent = _offer()
    source = "我出借；本金为100信用点；应还总额为110信用点；接受截止于世界第20秒；到期日为世界第30秒"

    assert credit_offer_terms_match(intent, source)
    assert not credit_offer_terms_match(
        _offer(currency_resource_id="feudal.silver_mark"), source,
    )

    before_epoch = _offer(accepted_through_tick=-20, maturity_tick=-10)
    assert credit_offer_terms_match(
        before_epoch,
        "提出借款方案。本金为100信用点。应还总额为110信用点。接受截止于世界第-20秒。到期于世界第负十秒。",
    )
    assert credit_offer_terms_match(
        intent,
        f"请提出借款方案，出借方机构 ID {intent.lender_organization_id}，借方机构 ID {intent.borrower_organization_id}，"
        "币种 modern.credit。本金 100。接受截止于世界第 20 秒。到期于世界第 30 秒。应还总额 110。",
    )


@pytest.mark.parametrize(
    "source",
    [
        "我出借；本金为101信用点；应还总额为110信用点；接受截止于第20秒；到期日为第30秒",
        "我出借；本金为100信用点；应还总额为110信用点；接受截止于第20秒；到期日为第31秒",
        "我出借；本金为100人民币；应还总额为110人民币；接受截止于第20秒；到期日为第30秒",
        "我出借；本金为100信用点；应还总额为110信用点；接受截止于第21秒；到期日为第30秒",
        "我考虑出借；本金为100信用点；应还总额为110信用点；接受截止于第20秒；到期日为第30秒",
        "我出借；本金为100信用点；应还总额为110信用点；接受截止于第20秒；到期日为第30秒；前提是先交保证金",
        "「我出借；本金为100信用点；应还总额为110信用点；接受截止于第20秒；到期日为第30秒」",
    ],
)
def test_credit_offer_rejects_changed_terms_or_uncommitted_language(source: str):
    assert not credit_offer_terms_match(_offer(), source)


def test_credit_acceptance_requires_direct_unquoted_consent():
    intent = _resolution("accept")

    assert credit_resolution_matches(
        intent, "确认接受这笔方案", remaining_minor=110, currency_resource_id=CURRENCY_ID,
    )
    for source in (
        "接受", "也许我接受这笔方案", "「确认接受这笔方案」", "有人提到我接受这笔方案",
        "我接受这笔，但如果账期不能延长就作废。", "如果能延期，我接受这笔。",
    ):
        assert not credit_resolution_matches(
            intent, source, remaining_minor=110, currency_resource_id=CURRENCY_ID,
        ), source


def test_credit_repayment_binds_exact_integer_and_currency():
    intent = _resolution("pay", 25)

    assert credit_resolution_matches(
        intent, "归还25信用点", remaining_minor=75, currency_resource_id=CURRENCY_ID,
    )
    assert credit_resolution_matches(
        intent, f"请还款 25 modern.credit，机构借款 {intent.credit_id}，版本 1。",
        remaining_minor=75, currency_resource_id=CURRENCY_ID,
    )
    assert not credit_resolution_matches(
        intent, "归还24信用点", remaining_minor=75, currency_resource_id=CURRENCY_ID,
    )
    assert not credit_resolution_matches(
        intent, "归还25人民币", remaining_minor=75, currency_resource_id=CURRENCY_ID,
    )
    assert not credit_resolution_matches(
        intent, "归还25银币", remaining_minor=75, currency_resource_id=CURRENCY_ID,
    )
    assert not credit_resolution_matches(
        intent, "考虑归还25信用点", remaining_minor=75, currency_resource_id=CURRENCY_ID,
    )
    assert not credit_resolution_matches(
        intent, "如果现金够，请归还25信用点。", remaining_minor=75, currency_resource_id=CURRENCY_ID,
    )
    assert not credit_resolution_matches(
        intent, "请暂不要归还25信用点。", remaining_minor=75, currency_resource_id=CURRENCY_ID,
    )
    assert not credit_resolution_matches(
        intent, "请解释归还25信用点的做法。", remaining_minor=75, currency_resource_id=CURRENCY_ID,
    )


def test_credit_full_repayment_phrase_only_matches_entire_remaining_balance():
    full = _resolution("pay", 75)
    partial = _resolution("pay", 25)

    assert credit_resolution_matches(
        full, "一次结清", remaining_minor=75, currency_resource_id=CURRENCY_ID,
    )
    assert not credit_resolution_matches(
        partial, "一次结清", remaining_minor=75, currency_resource_id=CURRENCY_ID,
    )
    assert not credit_resolution_matches(
        full, "「一次结清」", remaining_minor=75, currency_resource_id=CURRENCY_ID,
    )
    assert not credit_resolution_matches(
        full, "请不要一次结清。", remaining_minor=75, currency_resource_id=CURRENCY_ID,
    )


def test_truncated_contract_context_requires_the_exact_selected_id():
    intent = _resolution("accept")
    context = {
        "institutional_credit_contracts_truncated": True,
        "institutional_credit_contracts": [{
            "credit_id": str(intent.credit_id), "revision": 1, "can_accept": True,
            "remaining_total_minor": "75", "currency_resource_id": CURRENCY_ID,
        }],
    }
    assert not credit_action_matches_context(intent, "确认接受这笔方案。", context)
    assert credit_action_matches_context(intent, f"确认接受借款 {intent.credit_id}。", context)


def test_reserve_mobilization_requires_the_player_to_bind_organization_currency_and_quantity():
    organization_id = uuid4()
    intent = MobilizeTreasuryReserveIntent(
        organization_id=organization_id, currency_resource_id=CURRENCY_ID, amount_minor=4,
    )
    options = [{"organization_id": str(organization_id), "name": "青石织坊", "currency_resource_id": CURRENCY_ID}]
    command = f"请从机构 {organization_id} 的储备调入财库 4 modern.credit。"
    assert reserve_mobilization_matches_context(intent, command, options)
    assert reserve_mobilization_matches_context(intent, "请从青石织坊的储备中调入财库四个信用点。", options)
    for source in (
        "从青石织坊的储备调入财库 4 modern.credit，可以吗？",
        "请从青石织坊的储备调入财库 5 modern.credit。",
        "请从青石织坊的储备调入财库 4 feudal.silver_mark。",
        "请从青石织坊的储备调入财库 4 modern.credit，并还款。",
        "请从青石织坊的储备调入财库 4 modern.credit，如果库存够。",
        "他说，请从青石织坊的储备调入财库 4 modern.credit。",
        "请不要从青石织坊的储备调入财库 4 modern.credit。",
    ):
        assert not reserve_mobilization_matches_context(intent, source, options), source
    assert not reserve_mobilization_matches_context(intent, command, [])
    multiple_currencies = [*options, {**options[0], "currency_resource_id": "feudal.silver_mark"}]
    assert not reserve_mobilization_matches_context(intent, "请从青石织坊的储备调入财库4。", multiple_currencies)
    assert reserve_mobilization_matches_context(intent, command, multiple_currencies)

