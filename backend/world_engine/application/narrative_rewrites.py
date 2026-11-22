"""Account-scoped storage for completed prose rewrites and in-flight claims."""

from uuid import UUID

from world_engine.domain.errors import Conflict
from world_engine.domain.models import AccessScope

from .errors import ApplicationFailure
from .operations import OperationsStore


_MAX_NARRATIVES_PER_READ = 500


class NarrativeRewriteStore:
    def __init__(self, operations: OperationsStore):
        self.operations = operations

    def reserve(self, scope: AccessScope, narrative_id: str, style: str, claim_id: UUID) -> str | None:
        key = (*self.operations.identity(scope), narrative_id, style)
        with self.operations.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT text FROM narrative_rewrites WHERE tenant=? AND principal=? AND narrative_id=? AND style=?",
                key,
            ).fetchone()
            if row is not None:
                if row["text"] is not None:
                    return row["text"]
                raise ApplicationFailure("NARRATIVE_REWRITE_IN_PROGRESS", "这段旁白正在改写，请稍后查看。", retryable=True)
            db.execute(
                "INSERT INTO narrative_rewrites(tenant, principal, narrative_id, style, claim_id) VALUES(?,?,?,?,?)",
                (*key, str(claim_id)),
            )
        return None

    def finish(self, scope: AccessScope, narrative_id: str, style: str, claim_id: UUID, text: str) -> None:
        with self.operations.connect() as db:
            changed = db.execute(
                """UPDATE narrative_rewrites SET text=?, claim_id=NULL
                   WHERE tenant=? AND principal=? AND narrative_id=? AND style=? AND claim_id=?""",
                (text, *self.operations.identity(scope), narrative_id, style, str(claim_id)),
            ).rowcount
            if changed != 1:
                raise Conflict("The narrative rewrite claim no longer owns its completion")

    def discard(self, scope: AccessScope, narrative_id: str, style: str, claim_id: UUID) -> None:
        with self.operations.connect() as db:
            db.execute(
                """DELETE FROM narrative_rewrites
                   WHERE tenant=? AND principal=? AND narrative_id=? AND style=? AND claim_id=?""",
                (*self.operations.identity(scope), narrative_id, style, str(claim_id)),
            )

    def versions(self, scope: AccessScope, narrative_ids: list[str]) -> dict[str, dict[str, str]]:
        if not narrative_ids:
            return {}
        if len(narrative_ids) > _MAX_NARRATIVES_PER_READ:
            raise ValueError(f"A rewrite read allows up to {_MAX_NARRATIVES_PER_READ} narrative IDs")
        placeholders = ",".join("?" for _ in narrative_ids)
        versions: dict[str, dict[str, str]] = {}
        with self.operations.connect() as db:
            rows = db.execute(
                f"""SELECT narrative_id, style, text FROM narrative_rewrites
                    WHERE tenant=? AND principal=? AND narrative_id IN ({placeholders}) AND text IS NOT NULL""",
                (*self.operations.identity(scope), *narrative_ids),
            )
            for row in rows:
                versions.setdefault(row["narrative_id"], {})[row["style"]] = row["text"]
        return versions

    def recover_inflight(self) -> int:
        """Release interrupted claims after acquiring the exclusive runtime lock."""
        with self.operations.connect() as db:
            return db.execute("DELETE FROM narrative_rewrites WHERE claim_id IS NOT NULL").rowcount
