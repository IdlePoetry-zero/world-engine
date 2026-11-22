"""Bind one explicit user price ceiling to the current market currency.

This module does not infer purchase intent. Its caller is responsible for
invoking it only after the normal action dispatcher has validated a trade.
Only a single explicit upper-bound clause is accepted; the model proposal is
not evidence for the ceiling.
"""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import TYPE_CHECKING
import unicodedata

from pydantic import ValidationError

if TYPE_CHECKING:
    from world_engine.rules.contracts import TradePriceCeiling


PRICE_CEILING_AMBIGUOUS = "price_ceiling_ambiguous"
PRICE_CEILING_UNPARSEABLE = "price_ceiling_unparseable"
PRICE_CEILING_WRONG_CURRENCY = "price_ceiling_wrong_currency"
PRICE_CEILING_FRACTIONAL_MINOR_UNIT = "price_ceiling_fractional_minor_unit"
PRICE_CEILING_TOTAL_REQUIRED = "price_ceiling_total_required"


class PriceCeilingEvidenceError(ValueError):
    """A ceiling-like source phrase cannot safely bind to one integer total."""

    def __init__(self, code: str) -> None:
        if code not in {
            PRICE_CEILING_AMBIGUOUS,
            PRICE_CEILING_UNPARSEABLE,
            PRICE_CEILING_WRONG_CURRENCY,
            PRICE_CEILING_FRACTIONAL_MINOR_UNIT,
            PRICE_CEILING_TOTAL_REQUIRED,
        }:
            raise ValueError("Unknown price-ceiling evidence error code.")
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class _Marker:
    start: int
    end: int
    suffix: bool
    kind: str = "upper"


@dataclass(frozen=True, slots=True)
class _Amount:
    start: int
    end: int
    value: int


_PREFIX_MARKERS = (
    "不允许超过", "不得超过", "不能超过", "不可超过", "不要超过", "不超过",
    "不得高于", "不能高于", "不高于", "不多于", "至多", "最多", "最高", "上限", "封顶",
    "<=", "≤",
)
_SUFFIX_MARKERS = ("以内", "以下")
_RELATIONAL_MARKERS = (
    ("超过", "greater_rejection"),
    ("高于", "greater_rejection"),
    ("大于", "greater_rejection"),
    ("低于", "less_purchase"),
    ("小于", "less_purchase"),
    ("少于", "less_purchase"),
)
_ENGLISH_PREFIX_MARKER_PATTERNS = (
    re.compile(r"(?<![a-z])no\s+more\s+than(?![a-z])"),
    re.compile(r"(?<![a-z])at\s+most(?![a-z])"),
    re.compile(r"(?<![a-z])up\s+to(?![a-z])"),
)
_ENGLISH_SUFFIX_MARKER_PATTERNS = (
    re.compile(r"(?<![a-z])or\s+less(?![a-z])"),
    re.compile(r"(?<![a-z])or\s+below(?![a-z])"),
)

_NUMBER_TOKEN = r"(?:[0-9]+(?:\.[0-9]+)?|[零〇一二两三四五六七八九十百千万亿]+)"
_NUMBER_RE = re.compile(_NUMBER_TOKEN)
_RANGE_RE = re.compile(rf"{_NUMBER_TOKEN}\s*(?:到|至|[-~])\s*{_NUMBER_TOKEN}")
_PRICE_CONTEXT_TERMS = (
    "总价", "总额", "总共", "合计", "总计", "总花费", "花费", "费用", "价格", "价钱",
    "付款", "支付", "预算", "cost", "price", "spend", "budget", "total",
)
_PER_UNIT_TERMS = (
    "单价", "单位价", "每件", "每个", "每单位", "每份", "每斤", "每公斤", "每千克",
    "unit price", "price per unit", "per unit", "per item", "each",
)
_TOTAL_TERMS = ("总价", "总额", "总共", "合计", "总计", "总花费", "total price", "total cost", "total spend")
_PRICE_REFUSAL_TERMS = (
    "不买", "不购买", "别买", "不要买", "不能买", "不可买", "不下单", "不要下单",
    "不再购买", "暂不购买", "先别买", "停止购买", "不予购买",
)
_PRICE_PURCHASE_TERMS = (
    "才买", "才购买", "才下单", "才订购", "方可买", "方可购买", "才考虑买", "才考虑购买",
)
_CLAUSE_BREAKS = "。！？；;\n，,"

_BUNDLED_CURRENCY_ALIASES: dict[str, tuple[str, ...]] = {
    "feudal.silver_mark": (
        "silver marks", "silver mark", "银币", "银马克", "银马克币",
    ),
    "modern.credit": (
        "credits", "credit", "信用点", "信用点数", "信用币",
    ),
    "cultivation.spirit_stone": (
        "spirit stones", "spirit stone", "灵石", "灵石币",
    ),
}
_UNKNOWN_DENOMINATION_PATTERNS = (
    re.compile(r"人民币|美元|美金|港币|港元|元钱|块钱|银元|铜币|金币|硬币|日元|欧元"),
    re.compile(r"(?<![a-z])(?:usd|cny|rmb|hkd|eur|jpy|yuan|dollars?|bucks?|coins?)(?![a-z])"),
)
_UNKNOWN_DENOMINATION_SUFFIX = re.compile(r"^\s*(?:个|枚|单位|整)?\s*(?:人民币|美元|美金|港币|港元|元钱|块钱|元|块|钱|银元|铜币|金币|硬币|日元|欧元)")
_UNKNOWN_DENOMINATION_PREFIX = re.compile(r"(?:\$|¥|￥|€|£)\s*$")

_CHINESE_DIGITS = {
    "零": 0, "〇": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4,
    "五": 5, "六": 6, "七": 7, "八": 8, "九": 9,
}
_SMALL_UNITS = {"十": 10, "百": 100, "千": 1000}
_LARGE_UNITS = {"万": 10_000, "亿": 100_000_000}


def bind_trade_price_ceiling(
    source_text: str,
    *,
    local_currency_resource_id: str | None,
    local_currency_name: str | None = None,
) -> TradePriceCeiling | None:
    """Return the explicit total ceiling in the current local market currency.

    An unqualified number uses the current market's single registered currency.
    Any explicit denomination must resolve to that same currency. Unit-price
    ceilings are not converted into transaction totals.
    """

    if not isinstance(source_text, str) or not source_text.strip():
        return None
    text = unicodedata.normalize("NFKC", source_text).casefold()
    markers = _find_markers(text)
    if not markers:
        return None

    candidates: list[tuple[_Marker, str, _Amount, tuple[tuple[str, int, int], ...]]] = []
    saw_per_unit = False
    saw_unparseable_price_clause = False
    for marker in markers:
        clause_start, clause_end = _clause_bounds(text, marker.start, marker.end)
        clause = text[clause_start:clause_end]
        local_start, local_end = marker.start - clause_start, marker.end - clause_start
        context_window = clause[max(0, local_start - 32):min(len(clause), local_end + 48)]
        currency_mentions = _currency_mentions(clause, local_currency_resource_id, local_currency_name)
        has_price_context = (
            any(term in context_window for term in _PRICE_CONTEXT_TERMS)
            or bool(currency_mentions)
            or _unknown_currency_hint(clause)
        )
        if not has_price_context:
            continue
        if any(term in context_window for term in _PER_UNIT_TERMS):
            saw_per_unit = True
            continue
        amount = _amount_for_marker(clause, local_start, local_end, marker)
        if amount is None:
            saw_unparseable_price_clause = True
            continue
        if _RANGE_RE.search(clause):
            raise PriceCeilingEvidenceError(PRICE_CEILING_AMBIGUOUS)
        if _has_fractional_suffix(clause, amount):
            raise PriceCeilingEvidenceError(PRICE_CEILING_FRACTIONAL_MINOR_UNIT)
        if _has_competing_price_amount(clause, amount, currency_mentions):
            raise PriceCeilingEvidenceError(PRICE_CEILING_AMBIGUOUS)
        if _has_unbound_word_suffix(clause, amount, currency_mentions):
            raise PriceCeilingEvidenceError(PRICE_CEILING_WRONG_CURRENCY)
        if marker.kind == "greater_rejection":
            if not any(term in clause[amount.end:] for term in _PRICE_REFUSAL_TERMS):
                raise PriceCeilingEvidenceError(PRICE_CEILING_UNPARSEABLE)
        elif marker.kind == "less_purchase":
            if not any(term in clause[amount.end:] for term in _PRICE_PURCHASE_TERMS):
                raise PriceCeilingEvidenceError(PRICE_CEILING_UNPARSEABLE)
            if amount.value == 0:
                raise PriceCeilingEvidenceError(PRICE_CEILING_UNPARSEABLE)
            amount = _Amount(amount.start, amount.end, amount.value - 1)
        candidates.append((marker, clause, amount, currency_mentions))

    if saw_per_unit:
        raise PriceCeilingEvidenceError(PRICE_CEILING_TOTAL_REQUIRED)
    if saw_unparseable_price_clause:
        raise PriceCeilingEvidenceError(PRICE_CEILING_UNPARSEABLE)
    if len(candidates) > 1:
        raise PriceCeilingEvidenceError(PRICE_CEILING_AMBIGUOUS)
    if not candidates:
        return None

    _marker, clause, amount, currency_mentions = candidates[0]
    if any(term in clause for term in _PER_UNIT_TERMS) and not any(term in clause for term in _TOTAL_TERMS):
        raise PriceCeilingEvidenceError(PRICE_CEILING_TOTAL_REQUIRED)
    if not isinstance(local_currency_resource_id, str) or not local_currency_resource_id.strip():
        raise PriceCeilingEvidenceError(PRICE_CEILING_UNPARSEABLE)

    mentioned_ids = {resource_id for _alias, resource_id, _start in currency_mentions}
    if len(mentioned_ids) > 1:
        raise PriceCeilingEvidenceError(PRICE_CEILING_AMBIGUOUS)
    if mentioned_ids and local_currency_resource_id not in mentioned_ids:
        raise PriceCeilingEvidenceError(PRICE_CEILING_WRONG_CURRENCY)
    if _has_unknown_denomination(clause, amount, currency_mentions):
        raise PriceCeilingEvidenceError(PRICE_CEILING_WRONG_CURRENCY)

    from world_engine.rules.contracts import TradePriceCeiling

    try:
        return TradePriceCeiling(
            max_total_minor=amount.value,
            currency_resource_id=local_currency_resource_id,
        )
    except (ValidationError, TypeError, ValueError) as error:
        raise PriceCeilingEvidenceError(PRICE_CEILING_UNPARSEABLE) from error


def _find_markers(text: str) -> tuple[_Marker, ...]:
    found: list[_Marker] = []
    for value in _PREFIX_MARKERS:
        found.extend(_Marker(match.start(), match.end(), False) for match in re.finditer(re.escape(value), text))
    for value, kind in _RELATIONAL_MARKERS:
        found.extend(_Marker(match.start(), match.end(), False, kind) for match in re.finditer(re.escape(value), text))
    for value in _SUFFIX_MARKERS:
        found.extend(_Marker(match.start(), match.end(), True) for match in re.finditer(re.escape(value), text))
    for pattern in _ENGLISH_PREFIX_MARKER_PATTERNS:
        found.extend(_Marker(match.start(), match.end(), False) for match in pattern.finditer(text))
    for pattern in _ENGLISH_SUFFIX_MARKER_PATTERNS:
        found.extend(_Marker(match.start(), match.end(), True) for match in pattern.finditer(text))
    found.sort(key=lambda item: (item.start, -(item.end - item.start)))
    # Longer markers can contain shorter ones in future aliases; keep one
    # lexical hit per position while preserving distinct clauses/limits.
    deduplicated: list[_Marker] = []
    for item in found:
        if deduplicated and item.start < deduplicated[-1].end:
            continue
        deduplicated.append(item)
    return tuple(deduplicated)


def _clause_bounds(text: str, start: int, end: int) -> tuple[int, int]:
    left = max((text.rfind(ch, 0, start) + 1 for ch in _CLAUSE_BREAKS), default=0)
    right_candidates = [index for ch in _CLAUSE_BREAKS if (index := text.find(ch, end)) >= 0]
    return left, min(right_candidates) if right_candidates else len(text)


def _amount_for_marker(clause: str, start: int, end: int, marker: _Marker) -> _Amount | None:
    if marker.suffix:
        matches = list(_NUMBER_RE.finditer(clause, 0, start))
        if not matches:
            return None
        match = matches[-1]
    else:
        filler = r"\s*(?:(?:为|是|[:：])\s*)?" if clause[start:end] in {"最高", "上限", "封顶"} else r"\s*"
        prefix = re.match(filler, clause[end:])
        amount_start = end + (prefix.end() if prefix else 0)
        match = _NUMBER_RE.match(clause, amount_start)
        if match is None:
            return None
    token = match.group(0)
    value = parse_minor_amount(token)
    return _Amount(match.start(), match.end(), value)


def parse_minor_amount(token: str) -> int:
    """Read an exact integer from an already identified amount in player text."""
    if "." in token:
        raise PriceCeilingEvidenceError(PRICE_CEILING_FRACTIONAL_MINOR_UNIT)
    if token.isascii() and token.isdigit():
        return int(token)
    return _parse_chinese_integer(token)


def _parse_chinese_integer(token: str) -> int:
    if not token or any(ch not in _CHINESE_DIGITS and ch not in _SMALL_UNITS and ch not in _LARGE_UNITS for ch in token):
        raise PriceCeilingEvidenceError(PRICE_CEILING_UNPARSEABLE)
    if token in _CHINESE_DIGITS:
        return _CHINESE_DIGITS[token]

    total = 0
    section = 0
    number: int | None = None
    previous_char = ""
    smallest_unit = 10_000
    largest_unit = 2**63
    used_unit = False
    for char in token:
        if char in _CHINESE_DIGITS:
            digit = _CHINESE_DIGITS[char]
            if number is not None:
                if number == 0 and previous_char in {"零", "〇"}:
                    number = digit
                else:
                    raise PriceCeilingEvidenceError(PRICE_CEILING_UNPARSEABLE)
            else:
                number = digit
        elif char in _SMALL_UNITS:
            unit = _SMALL_UNITS[char]
            if unit >= smallest_unit:
                raise PriceCeilingEvidenceError(PRICE_CEILING_UNPARSEABLE)
            if number is None:
                if char == "十" and not used_unit and section == 0:
                    number = 1
                else:
                    raise PriceCeilingEvidenceError(PRICE_CEILING_UNPARSEABLE)
            section += number * unit
            number = None
            smallest_unit = unit
            used_unit = True
        else:
            unit = _LARGE_UNITS[char]
            if unit >= largest_unit:
                raise PriceCeilingEvidenceError(PRICE_CEILING_UNPARSEABLE)
            section += number or 0
            if section == 0:
                if total == 0:
                    section = 1
                else:
                    raise PriceCeilingEvidenceError(PRICE_CEILING_UNPARSEABLE)
            total += section * unit
            section = 0
            number = None
            smallest_unit = 10_000
            largest_unit = unit
            used_unit = True
        previous_char = char
    return total + section + (number or 0)


def _has_fractional_suffix(clause: str, amount: _Amount) -> bool:
    suffix = clause[amount.end:amount.end + 8]
    return bool(re.match(r"\s*(?:点|\.\s*[0-9]|半)", suffix))


def _has_competing_price_amount(
    clause: str,
    amount: _Amount,
    currency_mentions: tuple[tuple[str, str, int], ...],
) -> bool:
    for candidate in _NUMBER_RE.finditer(clause):
        if candidate.start() == amount.start and candidate.end() == amount.end:
            continue
        for alias, _resource_id, start in currency_mentions:
            end = start + len(alias)
            after_amount = clause[candidate.end():start]
            after_currency = clause[end:candidate.start()]
            if candidate.end() <= start and re.fullmatch(r"\s*(?:个|枚|单位|整)?\s*", after_amount):
                return True
            if end <= candidate.start() and re.fullmatch(r"\s*(?:或|或者|or|and|以及|及)?\s*", after_currency):
                return True
    return False


def _currency_mentions(
    clause: str,
    local_currency_resource_id: str | None,
    local_currency_name: str | None,
) -> tuple[tuple[str, str, int], ...]:
    mentions: list[tuple[str, str, int]] = []
    aliases_by_id = dict(_BUNDLED_CURRENCY_ALIASES)
    if isinstance(local_currency_resource_id, str) and local_currency_resource_id:
        aliases_by_id[local_currency_resource_id] = tuple(dict.fromkeys((
            *aliases_by_id.get(local_currency_resource_id, ()),
            local_currency_resource_id,
            *((local_currency_name,) if isinstance(local_currency_name, str) and local_currency_name.strip() else ()),
        )))
    for resource_id, aliases in aliases_by_id.items():
        for alias in aliases:
            if not alias:
                continue
            pattern = _alias_pattern(alias)
            mentions.extend((alias, resource_id, match.start()) for match in pattern.finditer(clause))
    mentions.sort(key=lambda row: row[2])
    return tuple(mentions)


def _alias_pattern(alias: str) -> re.Pattern[str]:
    escaped = re.escape(alias.casefold())
    if alias.isascii() and any(ch.isalnum() for ch in alias):
        return re.compile(rf"(?<![a-z0-9_.]){escaped}(?![a-z0-9_.])", re.IGNORECASE)
    return re.compile(escaped, re.IGNORECASE)


def _unknown_currency_hint(clause: str) -> bool:
    return any(pattern.search(clause) for pattern in _UNKNOWN_DENOMINATION_PATTERNS)


def _has_unknown_denomination(
    clause: str,
    amount: _Amount,
    known_mentions: tuple[tuple[str, str, int], ...],
) -> bool:
    known_spans: list[tuple[int, int]] = []
    for alias, _resource_id, start in known_mentions:
        known_spans.append((start, start + len(alias)))
    for pattern in _UNKNOWN_DENOMINATION_PATTERNS:
        for match in pattern.finditer(clause):
            if not any(start <= match.start() and match.end() <= end for start, end in known_spans):
                return True
    # Check denomination-like symbols only beside the parsed amount. A symbol
    # elsewhere in the source is not enough to identify this ceiling's unit.
    prefix = clause[max(0, amount.start - 12):amount.start]
    suffix = clause[amount.end:amount.end + 24]
    if _UNKNOWN_DENOMINATION_PREFIX.search(prefix):
        return True
    if _UNKNOWN_DENOMINATION_SUFFIX.match(suffix):
        return True
    return False


def _has_unbound_word_suffix(
    clause: str,
    amount: _Amount,
    known_mentions: tuple[tuple[str, str, int], ...],
) -> bool:
    prefix = re.match(r"\s*(?:(?:个|枚|单位|整)\s*)?", clause[amount.end:])
    suffix_start = amount.end + prefix.end()
    suffix = clause[suffix_start:]
    if any(amount.end <= start <= suffix_start for _alias, _resource_id, start in known_mentions):
        return False
    # A connector can follow a genuinely omitted denomination. Other words
    # here must match a currency name, including names without a classifier.
    if suffix.startswith(("就", "才", "则", "便", "时", "后", "即可", "可以", "不能", "不买", "别买", "以内", "以下", "给我")):
        return False
    if re.match(r"(?:total|then|otherwise|with|and buy|in total|to buy)\b", suffix):
        return False
    return re.match(r"[a-z\u4e00-\u9fff]", suffix) is not None

