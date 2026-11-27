"""Player-owned, revisioned preparation for actions that need a second turn."""

import json
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from world_engine.domain.errors import Conflict, NotFound
from world_engine.rules.contracts import ProposeRelationshipInvitationIntent, ResolveInstitutionalCreditIntent, TradeIntent


class CreditResolutionSelection(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["resolve_institutional_credit"]
    credit_id: UUID
    operation: Literal["accept", "decline", "withdraw", "pay"]
    expected_revision: Annotated[str, Field(pattern=r"^[1-9][0-9]{0,18}$")]
    amount_minor: Annotated[str, Field(pattern=r"^[1-9][0-9]{0,29}$")] | None = None

    @model_validator(mode="after")
    def payment_and_revision(self):
        if (self.operation == "pay") != (self.amount_minor is not None):
            raise ValueError("A payment requires a positive amount in minor units.")
        if int(self.expected_revision) > 2**63 - 1:
            raise ValueError("The credit revision exceeds the supported range.")
        return self

    def rule_intent(self) -> ResolveInstitutionalCreditIntent:
        return ResolveInstitutionalCreditIntent(
            credit_id=self.credit_id, operation=self.operation, expected_revision=int(self.expected_revision),
            amount_minor=int(self.amount_minor) if self.amount_minor is not None else None,
        )


class DraftControl(BaseModel):
    model_config = ConfigDict(extra="forbid")

    operation: Literal["prepare", "update", "confirm", "cancel"]
    draft_id: UUID | None = None
    revision: Annotated[int, Field(strict=True, ge=1)] | None = None

    @model_validator(mode="after")
    def versioned_reference(self):
        if self.operation == "prepare":
            if self.draft_id is not None or self.revision is not None:
                raise ValueError("Preparation must start a new draft.")
        elif self.draft_id is None or self.revision is None:
            raise ValueError("Draft updates, confirmations and cancellations require its ID and revision.")
        return self


class ActionDraft(BaseModel):
    """Only reviewed authorizations enter this document, never model reasoning."""

    model_config = ConfigDict(extra="forbid")

    draft_id: UUID
    location_id: UUID | None
    revision: int = 1
    state_version: int
    action: Annotated[
        TradeIntent | ProposeRelationshipInvitationIntent | ResolveInstitutionalCreditIntent, Field(discriminator="kind"),
    ] | None
    source_text: str
    review: str

    def readback(self) -> dict:
        return {
            "draft_id": str(self.draft_id),
            "revision": self.revision,
            "action_kind": self.action.kind if self.action else None,
            "can_confirm": self.action is not None,
            "review": self.review,
        }


class ActionDraftStore:
    def __init__(self, operations):
        self.operations = operations
        with operations.connect() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS action_drafts (
                tenant TEXT NOT NULL, principal TEXT NOT NULL, world_id TEXT NOT NULL,
                branch_id TEXT NOT NULL, session_id TEXT NOT NULL, actor_id TEXT NOT NULL,
                draft_id TEXT NOT NULL, revision INTEGER NOT NULL, document TEXT NOT NULL,
                status TEXT NOT NULL CHECK (status IN ('open', 'confirming', 'confirmed', 'cancelled')),
                claimant TEXT,
                PRIMARY KEY (tenant, principal, world_id, branch_id, session_id, actor_id, draft_id)
            )""")

    def _key(self, scope, request, draft_id):
        return (*self.operations.identity(scope), str(request.world_id), str(request.branch_id),
                str(request.session_id), str(request.actor_id), str(draft_id))

    _WHERE = "tenant=? AND principal=? AND world_id=? AND branch_id=? AND session_id=? AND actor_id=? AND draft_id=?"

    def latest(self, scope, world_id, branch_id, session_id, actor_id) -> ActionDraft | None:
        with self.operations.connect() as db:
            row = db.execute("""SELECT document FROM action_drafts
                WHERE tenant=? AND principal=? AND world_id=? AND branch_id=? AND session_id=? AND actor_id=?
                AND status='open' ORDER BY rowid DESC LIMIT 1""",
                (*self.operations.identity(scope), str(world_id), str(branch_id), str(session_id), str(actor_id)),
            ).fetchone()
        return ActionDraft.model_validate_json(row["document"]) if row else None

    def read(self, scope, request, control: DraftControl) -> ActionDraft:
        with self.operations.connect() as db:
            row = db.execute(f"SELECT document, status FROM action_drafts WHERE {self._WHERE}",
                             self._key(scope, request, control.draft_id)).fetchone()
        if row is None:
            raise NotFound("Action draft not found.")
        draft = ActionDraft.model_validate_json(row["document"])
        if row["status"] != "open" or draft.revision != control.revision:
            raise Conflict("草稿已更新或关闭，请使用最新的待确认内容。")
        return draft

    def save(self, scope, request, draft: ActionDraft, *, previous_revision: int | None = None):
        key = self._key(scope, request, draft.draft_id)
        with self.operations.connect() as db:
            if previous_revision is None:
                changed = db.execute(
                    "INSERT OR IGNORE INTO action_drafts VALUES (?,?,?,?,?,?,?,?,?,'open',?)",
                    (*key, draft.revision, draft.model_dump_json(), str(request.request_id)),
                ).rowcount
            else:
                changed = db.execute(
                    f"""UPDATE action_drafts SET revision=?, document=?, claimant=? WHERE {self._WHERE}
                    AND revision=? AND status='open'""",
                    (draft.revision, draft.model_dump_json(), str(request.request_id), *key, previous_revision),
                ).rowcount
            if changed != 1:
                raise Conflict("草稿已更新或关闭，请使用最新的待确认内容。")

    def claim(self, scope, request, draft: ActionDraft, *, cancel: bool = False):
        status = "cancelled" if cancel else "confirming"
        with self.operations.connect() as db:
            changed = db.execute(
                f"""UPDATE action_drafts SET status=?, claimant=? WHERE {self._WHERE}
                AND revision=? AND status='open'""",
                (status, str(request.request_id), *self._key(scope, request, draft.draft_id), draft.revision),
            ).rowcount
        if changed != 1:
            raise Conflict("草稿已更新或关闭，请使用最新的待确认内容。")

    def finish(self, scope, request, draft: ActionDraft, *, committed: bool):
        status = "confirmed" if committed else "open"
        with self.operations.connect() as db:
            db.execute(
                f"""UPDATE action_drafts SET status=?, claimant=? WHERE {self._WHERE}
                AND revision=? AND status='confirming' AND claimant=?""",
                (status, str(request.request_id) if committed else None,
                 *self._key(scope, request, draft.draft_id), draft.revision, str(request.request_id)),
            )

    def receipt(self, scope, world_id, branch_id, actor_id, request_id):
        with self.operations.connect() as db:
            row = db.execute("""SELECT session_id, document, status FROM action_drafts
                WHERE tenant=? AND principal=? AND world_id=? AND branch_id=? AND actor_id=? AND claimant=?""",
                (*self.operations.identity(scope), str(world_id), str(branch_id), str(actor_id), str(request_id)),
            ).fetchone()
        return row

    def reconcile(self, scope, world_id, branch_id, actor_id, session_id, draft_id, request_id, *, committed):
        with self.operations.connect() as db:
            db.execute(f"""UPDATE action_drafts SET status=?, claimant=?
                WHERE {self._WHERE} AND status='confirming' AND claimant=?""",
                ("confirmed" if committed else "open", str(request_id) if committed else None,
                 *self.operations.identity(scope), str(world_id), str(branch_id), session_id, str(actor_id),
                 str(draft_id), str(request_id)),
            )
