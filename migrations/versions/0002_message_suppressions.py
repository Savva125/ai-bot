"""Prevent deleted and expired messages from being reimported."""
from alembic import op
import sqlalchemy as sa

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "message_suppressions",
        sa.Column("chat_id", sa.String(36), nullable=False),
        sa.Column("external_hash", sa.String(64), nullable=False),
        sa.ForeignKeyConstraint(["chat_id"], ["chats.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("chat_id", "external_hash"),
    )


def downgrade():
    op.drop_table("message_suppressions")
