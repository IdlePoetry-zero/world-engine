"""Preserve the source archive of worlds created from imported packages.

Revision ID: c8d04ac3f19a
Revises: 6fbc6e5d1a70
Create Date: 2026-11-22
"""

from alembic import op
import sqlalchemy as sa


revision = "c8d04ac3f19a"
down_revision = "6fbc6e5d1a70"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "world_pack_archives",
        sa.Column("world_id", sa.Uuid(), nullable=False),
        sa.Column("archive_bytes", sa.LargeBinary(), nullable=False),
        sa.CheckConstraint(
            "length(archive_bytes) BETWEEN 1 AND 16777216",
            name=op.f("ck_world_pack_archives_archive_size_bounded"),
        ),
        sa.ForeignKeyConstraint(
            ["world_id"],
            ["worlds.world_id"],
            name=op.f("fk_world_pack_archives_world_id_worlds"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("world_id", name=op.f("pk_world_pack_archives")),
    )


def downgrade() -> None:
    op.drop_table("world_pack_archives")
