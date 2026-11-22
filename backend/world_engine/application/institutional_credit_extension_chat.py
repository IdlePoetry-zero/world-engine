"""Bind deadline changes to the player's explicit loan, terms and choice."""

import re
import unicodedata

from world_engine.rules.contracts import ProposeCreditExtensionIntent, ResolveCreditExtensionIntent

from .institutional_credit_chat import _CLAUSE, _CONDITIONAL, _TICK, _ticks


_PROPOSAL = re.compile(r"^(?:我\s*)?代表\s*(?:机构\s*)?(?P<organization>.+?)\s*提出借款\s*(?P<loan>[0-9a-f-]{36})\s*的?展期方案$")
_NEW_DEADLINE = re.compile(rf"新的到期(?:时刻|日期|期限)?\s*(?:为|是|至)?\s*(?:世界)?第?\s*{_TICK}\s*秒")
_ACCEPT_CUTOFF = re.compile(rf"接受截止(?:于)?\s*(?:世界)?第?\s*{_TICK}\s*秒")
_RESOLUTION = re.compile(r"^(?:我\s*)?(?P<choice>接受|拒绝|撤回)借款展期方案\s*(?P<proposal>[0-9a-f-]{36})$")
_CHOICES = {"接受": "accept", "拒绝": "decline", "撤回": "withdraw"}


def credit_extension_action_matches(
    action: ProposeCreditExtensionIntent | ResolveCreditExtensionIntent, text: str, context: dict,
) -> bool:
    source = unicodedata.normalize("NFKC", text)
    if _CONDITIONAL.search(source):
        return False
    clauses = [(segment.strip(), ending) for segment, ending in _CLAUSE.findall(source) if segment.strip()]
    if not clauses or any(ending == "?" for _, ending in clauses):
        return False
    contract = next((row for row in context.get("institutional_credit_contracts", ())
                     if row["credit_id"] == str(action.credit_id) and row["revision"] == action.expected_revision), None)
    if contract is None:
        return False
    first = clauses[0][0]
    if isinstance(action, ProposeCreditExtensionIntent):
        proposal = _PROPOSAL.fullmatch(first)
        if proposal is None or proposal["loan"] != str(action.credit_id):
            return False
        organizations = {
            row["organization_id"] for row in contract["extension_options"]
            if proposal["organization"].strip() in {row["organization_id"], row["name"]}
        }
        if organizations != {str(action.organization_id)} or len(clauses) != 3:
            return False
        try:
            return (
                _NEW_DEADLINE.fullmatch(clauses[1][0]) is not None
                and _ACCEPT_CUTOFF.fullmatch(clauses[2][0]) is not None
                and _ticks(_NEW_DEADLINE, clauses[1][0]) == {action.new_maturity_tick}
                and _ticks(_ACCEPT_CUTOFF, clauses[2][0]) == {action.accept_through_tick}
            )
        except ValueError:
            return False
    resolution = _RESOLUTION.fullmatch(first)
    if resolution is None or len(clauses) != 1 or _CHOICES[resolution["choice"]] != action.operation:
        return False
    if resolution["proposal"] != str(action.extension_id):
        return False
    return any(row["extension_id"] == str(action.extension_id) and row[f"can_{action.operation}"]
               for row in contract["extensions"])
