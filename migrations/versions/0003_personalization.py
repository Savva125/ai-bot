"""Assistant style and operator-approved reply examples."""
from alembic import op
import sqlalchemy as sa

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("assistant_settings", sa.Column("key", sa.String(80), primary_key=True),
                    sa.Column("value", sa.Text(), nullable=False))
    op.create_table("reply_examples",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("account_id", sa.String(36), sa.ForeignKey("accounts.id", ondelete="CASCADE"), nullable=False),
        sa.Column("draft_id", sa.String(36), sa.ForeignKey("drafts.id", ondelete="SET NULL"), nullable=True),
        sa.Column("context", sa.Text(), nullable=False), sa.Column("proposed", sa.Text(), nullable=False),
        sa.Column("reply", sa.Text(), nullable=False), sa.Column("note", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False))
    op.create_index("ix_reply_examples_account_id", "reply_examples", ["account_id"])


def downgrade():
    op.drop_index("ix_reply_examples_account_id", "reply_examples")
    op.drop_table("reply_examples")
    op.drop_table("assistant_settings")
