"""Ordered speaker-aware conversation context and automatic follow-up allowance."""
from alembic import op
import sqlalchemy as sa

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("conversation_contexts",
        sa.Column("chat_id", sa.String(36), sa.ForeignKey("chats.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("turns", sa.JSON(), nullable=False),
        sa.Column("last_incoming_key", sa.String(64), nullable=False),
        sa.Column("followup_anchor", sa.String(64), nullable=False))


def downgrade():
    op.drop_table("conversation_contexts")
