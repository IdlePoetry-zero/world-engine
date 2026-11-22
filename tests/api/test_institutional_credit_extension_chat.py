"""Deadline changes require the exact contract, terms, and player decision."""

from uuid import uuid4

from world_engine.application.institutional_credit_extension_chat import credit_extension_action_matches
from world_engine.rules.contracts import ProposeCreditExtensionIntent, ResolveCreditExtensionIntent


def test_proposal_binds_identified_institution_and_both_player_supplied_deadlines():
    credit_id, organization_id = uuid4(), uuid4()
    context = {"institutional_credit_contracts": [{
        "credit_id": str(credit_id), "revision": 3, "extension_options": [
            {"organization_id": str(organization_id), "name": "青石织坊"},
        ],
    }]}
    action = ProposeCreditExtensionIntent(credit_id=credit_id, organization_id=organization_id, expected_revision=3,
                                        new_maturity_tick=300, accept_through_tick=150)
    draft = f"我代表机构 {organization_id} 提出借款 {credit_id} 的展期方案。新的到期时刻为世界第 300 秒，接受截止于世界第 150 秒。"
    assert credit_extension_action_matches(action, draft, context)
    assert credit_extension_action_matches(action, draft.replace(str(organization_id), "青石织坊"), context)
    assert not credit_extension_action_matches(action, draft.replace("150", "160"), context)
    assert not credit_extension_action_matches(action, draft.replace("。", "？", 1), context)
    assert not credit_extension_action_matches(action, draft + "前提是减免本金。", context)
    assert not credit_extension_action_matches(action.model_copy(update={"expected_revision": 4}), draft, context)


def test_resolution_requires_only_the_explicit_choice_and_exact_authorized_proposal():
    credit_id, extension_id = uuid4(), uuid4()
    entry = {"extension_id": str(extension_id), "can_accept": True, "can_decline": False, "can_withdraw": False}
    context = {"institutional_credit_contracts": [{"credit_id": str(credit_id), "revision": 4, "extensions": [entry]}]}
    action = ResolveCreditExtensionIntent(credit_id=credit_id, extension_id=extension_id,
                                        expected_revision=4, operation="accept")
    assert credit_extension_action_matches(action, f"我接受借款展期方案 {extension_id}。", context)
    assert not credit_extension_action_matches(action, f"我接受借款展期方案 {uuid4()}。", context)
    assert not credit_extension_action_matches(action, f"我接受借款展期方案 {extension_id}。同时请还款。", context)
    assert not credit_extension_action_matches(action, f"我拒绝借款展期方案 {extension_id}。", context)
    assert not credit_extension_action_matches(action, f"我接受借款展期方案 {extension_id}？", context)
    entry["can_accept"] = False
    assert not credit_extension_action_matches(action, f"我接受借款展期方案 {extension_id}。", context)
