"""Add a side table for lossless compressed event payload bytes.

Revision ID: 6fbc6e5d1a70
Revises: bdc44c16f77a
Create Date: 2026-11-15
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "6fbc6e5d1a70"
down_revision: Union[str, None] = "bdc44c16f77a"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "world_event_payloads",
        sa.Column("event_id", sa.Uuid(), nullable=False),
        sa.Column("compressed_bytes", sa.LargeBinary(), nullable=False),
        sa.Column("raw_byte_count", sa.Integer(), nullable=False),
        sa.Column("codec_version", sa.Integer(), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.CheckConstraint(
            "codec_version >= 1",
            name=op.f("ck_world_event_payloads_codec_version_positive"),
        ),
        sa.CheckConstraint(
            "raw_byte_count > 0 AND raw_byte_count <= 67108864",
            name=op.f("ck_world_event_payloads_raw_byte_count_bounded_positive"),
        ),
        sa.CheckConstraint(
            "length(sha256) = 64",
            name=op.f("ck_world_event_payloads_sha256_length"),
        ),
        sa.CheckConstraint(
            "length(compressed_bytes) > 0",
            name=op.f("ck_world_event_payloads_compressed_payload_nonempty"),
        ),
        sa.ForeignKeyConstraint(
            ["event_id"],
            ["world_events.event_id"],
            name=op.f("fk_world_event_payloads_event_id_world_events"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("event_id", name=op.f("pk_world_event_payloads")),
    )


def downgrade() -> None:
    op.drop_table("world_event_payloads")
