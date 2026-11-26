"""Index the actor's direct movement receipts used by historical station progress."""

from alembic import op
import sqlalchemy as sa

revision = "e0647dcbe392"
down_revision = "c8d04ac3f19a"
branch_labels = None
depends_on = None


def upgrade() -> None:
    direct_move = sa.text("event_type = 'core.movement_completed.v1'")
    op.create_index(
        "ix_events_direct_move_branch_sequence", "world_events", ["world_id", "branch_id", "sequence"],
        sqlite_where=direct_move, postgresql_where=direct_move,
    )


def downgrade() -> None:
    op.drop_index("ix_events_direct_move_branch_sequence", table_name="world_events")
