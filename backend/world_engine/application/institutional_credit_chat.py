"""Ground financial instructions in the user's words before rule evaluation."""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Mapping

from world_engine.rules.contracts import MobilizeTreasuryReserveIntent, OfferInstitutionalCreditIntent, ResolveInstitutionalCreditIntent

from .market_price_ceiling import bind_trade_price_ceiling, parse_minor_amount


_UNSIGNED_NUMBER = r"(?:[0-9]+(?:\.[0-9]+)?|[零〇一二两三四五六七八九十百千万亿]+)"
_NUMBER = rf"({_UNSIGNED_NUMBER})"
_TICK = rf"([-负]?{_UNSIGNED_NUMBER})"
_PRINCIPAL = re.compile(rf"本金\s*(?:是|为|定为)?\s*{_NUMBER}")
_TOTAL = re.compile(rf"(?:应还总额|总偿还额|总共归还)\s*(?:是|为|定为)?\s*{_NUMBER}")
_ACCEPT_THROUGH = re.compile(rf"(?:接受截止(?:于)?|接受期限(?:至)?)\s*(?:世界)?第?\s*{_TICK}\s*秒")
_MATURITY = re.compile(rf"(?:到期(?:日为|于)?|还款截止(?:于)?|偿还期限(?:至)?)\s*(?:世界)?第?\s*{_TICK}\s*秒")
_PAYMENT = re.compile(rf"(?:归还|偿还|补还|还款|偿付)\s*(?:剩余|剩下的|本金)?\s*{_NUMBER}")
_PAYMENT_LEAD = re.compile(r"^(?:请\s*)?(?:(?:我现在|我决定|现在|立即|马上)\s*)?")
_FULL_PAYMENT = ("结清这笔", "结清借款", "全部归还", "还清这笔", "一次结清")
_ACCEPT = ("确认接受", "我接受", "请接受", "决定接受", "同意接受", "我同意接受", "正式接受", "请正式接受")
_DECLINE = ("确认拒绝", "我拒绝", "请拒绝", "决定拒绝", "不接受这笔", "我不接受这笔")
_WITHDRAW = ("确认撤回", "请撤回", "现在撤回", "我撤回", "决定撤回")
_CLAUSE = re.compile(r"([^，。！？,!?；;\n]+)([，。！？,!?；;\n]?)")
_OFFER = ("提出借款方案", "发出借款方案", "请提出借款方案", "请发出借款方案", "我出借", "请出借", "出借")
_CONDITIONAL = re.compile(r"如果|假如|假设|除非|倘若|前提是|以.{1,80}为前提|才(?:同意|接受|拒绝|出借|还款|偿还|撤回)")
_RESERVE_COMMAND = re.compile(
    rf"^(?:请\s*)?(?:现在\s*)?从\s*(?:机构\s*)?(?P<organization>.+?)\s*(?:的\s*)?"
    rf"储备(?:现金|账户)?(?:中)?\s*(?:调|划|转|拨)入\s*(?:本机构\s*)?财库\s*(?P<amount>{_UNSIGNED_NUMBER})"
)
_RESERVE_MIXED = re.compile(r"(?:并|再|然后|接着|同时)\s*(?:顺便)?\s*(?:还款|归还|偿还|结清|支付|调入|划入|拨入|转入)")


def _ticks(pattern: re.Pattern[str], source: str) -> set[int]:
    values = set()
    for value in pattern.findall(source):
        if value.startswith(("-", "负")):
            values.add(-parse_minor_amount(value[1:]))
        else:
            values.add(parse_minor_amount(value))
    return values


def _money_terms(pattern: re.Pattern[str], clauses: list[str], currency: str) -> set[int]:
    amounts = set()
    for clause in clauses:
        for match in pattern.finditer(clause):
            quoted = bind_trade_price_ceiling("总价不超过" + clause[match.start(1):], local_currency_resource_id=currency)
            if quoted is not None:
                amounts.add(quoted.max_total_minor)
    return amounts


def credit_offer_terms_match(intent: OfferInstitutionalCreditIntent, source: str) -> bool:
    source = unicodedata.normalize("NFKC", source)
    if _CONDITIONAL.search(source):
        return False
    clauses = [text.strip() for text, end in _CLAUSE.findall(source) if end not in {"?", "？"}]
    try:
        totals = _money_terms(_TOTAL, clauses, intent.currency_resource_id)
        expected_total = {intent.total_repayable_minor} if intent.total_repayable_minor is not None else set()
        return (
            any(text.startswith(_OFFER) for text in clauses)
            and _money_terms(_PRINCIPAL, clauses, intent.currency_resource_id) == {intent.principal_minor}
            and _ticks(_ACCEPT_THROUGH, source) == {intent.accepted_through_tick}
            and _ticks(_MATURITY, source) == {intent.maturity_tick}
            and totals == expected_total
        )
    except ValueError:
        return False


def credit_resolution_matches(
    intent: ResolveInstitutionalCreditIntent, source: str, *, remaining_minor: int, currency_resource_id: str,
) -> bool:
    source = unicodedata.normalize("NFKC", source)
    if _CONDITIONAL.search(source):
        return False
    clauses = [text.strip() for text, end in _CLAUSE.findall(source) if end not in {"?", "？"}]
    if intent.operation == "pay":
        payment_clauses = [_PAYMENT_LEAD.sub("", text, count=1) for text in clauses]
        try:
            amounts = _money_terms(
                _PAYMENT, [text for text in payment_clauses if _PAYMENT.match(text)], currency_resource_id,
            )
        except ValueError:
            return False
        if amounts:
            return amounts == {intent.amount_minor}
        return intent.amount_minor == remaining_minor and any(
            text.startswith(_FULL_PAYMENT) for text in payment_clauses
        )
    words = {"accept": _ACCEPT, "decline": _DECLINE, "withdraw": _WITHDRAW}[intent.operation]
    return any(text.startswith(words) for text in clauses)


def credit_action_matches_context(
    intent: OfferInstitutionalCreditIntent | ResolveInstitutionalCreditIntent,
    source: str, context: Mapping[str, object],
) -> bool:
    if isinstance(intent, OfferInstitutionalCreditIntent):
        if not credit_offer_terms_match(intent, source):
            return False
        represented = context.get("represented_institutions", ())
        if len(represented) > 1 and not any(
            row["organization_id"] == str(intent.lender_organization_id)
            and (row["organization_id"] in source or (row["name"] and row["name"] in source))
            for row in represented
        ):
            return False
        return any(
            row["represented_organization_id"] == str(intent.lender_organization_id)
            and row["counterparty_organization_id"] == str(intent.borrower_organization_id)
            and (row["counterparty_organization_id"] in source or (row["counterparty_name"] and row["counterparty_name"] in source))
            and intent.currency_resource_id in row["shared_currency_resource_ids"]
            for row in context.get("visible_credit_counterparties", ())
        )

    contracts = context.get("institutional_credit_contracts", ())
    consent_flag = f"can_{intent.operation}"
    mentioned = [row for row in contracts if row["credit_id"] in source]
    if not mentioned and context.get("institutional_credit_contracts_truncated"):
        return False
    options = mentioned or [row for row in contracts if row[consent_flag]]
    if len(options) != 1:
        return False
    option = options[0]
    return (
        option["credit_id"] == str(intent.credit_id)
        and option["revision"] == intent.expected_revision
        and option[consent_flag]
        and credit_resolution_matches(
            intent, source, remaining_minor=int(option["remaining_total_minor"]),
            currency_resource_id=option["currency_resource_id"],
        )
    )


def reserve_mobilization_matches_context(
    intent: MobilizeTreasuryReserveIntent, source: str, options: list[dict[str, object]],
) -> bool:
    source = unicodedata.normalize("NFKC", source)
    if _CONDITIONAL.search(source) or _RESERVE_MIXED.search(source):
        return False
    clauses = [(text.strip(), end) for text, end in _CLAUSE.findall(source) if text.strip()]
    if not clauses or any(end == "?" for _, end in clauses):
        return False
    text, _ = clauses[0]
    match = _RESERVE_COMMAND.match(text)
    if match is None or any(clause.endswith(("吗", "么", "呢")) for clause, _ in clauses):
        return False
    if any(_RESERVE_COMMAND.match(clause) for clause, _ in clauses[1:]):
        return False
    name = match["organization"].strip()
    selected = [row for row in options if name in {row["organization_id"], row["name"]}]
    if not selected or {row["organization_id"] for row in selected} != {str(intent.organization_id)}:
        return False
    currency = intent.currency_resource_id
    if currency not in {row["currency_resource_id"] for row in selected}:
        return False
    if len(selected) > 1 and currency not in text[match.end():].split():
        return False
    try:
        amount = bind_trade_price_ceiling(
            "总价不超过" + text[match.start("amount"):], local_currency_resource_id=currency,
        )
    except ValueError:
        return False
    return amount is not None and amount.max_total_minor == intent.amount_minor
