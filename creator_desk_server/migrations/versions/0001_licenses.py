"""Initial hosted licensing schema, independent from desktop history."""
from alembic import op
import sqlalchemy as sa

revision = "server_0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("licenses",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("key_hash", sa.String(64), nullable=False, unique=True),
        sa.Column("label", sa.String(120), nullable=False),
        sa.Column("days", sa.Integer, nullable=False),
        sa.Column("daily_limit", sa.Integer, nullable=False),
        sa.Column("max_devices", sa.Integer, nullable=False),
        sa.Column("created_at", sa.BigInteger, nullable=False),
        sa.Column("expires_at", sa.BigInteger, nullable=True),
        sa.Column("blocked", sa.Boolean, nullable=False))
    op.create_table("license_devices",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("license_id", sa.String(36), sa.ForeignKey("licenses.id"), nullable=False),
        sa.Column("device_hash", sa.String(64), nullable=False),
        sa.Column("token_hash", sa.String(64), nullable=False, unique=True),
        sa.Column("expires_at", sa.BigInteger, nullable=False),
        sa.UniqueConstraint("license_id", "device_hash"))
    op.create_index("ix_license_devices_license_id", "license_devices", ["license_id"])
    op.create_table("generation_jobs",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("license_id", sa.String(36), sa.ForeignKey("licenses.id"), nullable=False),
        sa.Column("request_id", sa.String(36), nullable=False),
        sa.Column("context_hash", sa.String(64), nullable=False),
        sa.Column("created_at", sa.BigInteger, nullable=False),
        sa.Column("lease_until", sa.BigInteger, nullable=False),
        sa.Column("state", sa.String(16), nullable=False),
        sa.Column("result", sa.Text, nullable=True),
        sa.UniqueConstraint("license_id", "request_id"))
    op.create_index("ix_generation_jobs_license_id", "generation_jobs", ["license_id"])
    op.create_index("ix_generation_jobs_created_at", "generation_jobs", ["created_at"])
    op.create_table("license_audit",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("actor", sa.String(80), nullable=False),
        sa.Column("action", sa.String(32), nullable=False),
        sa.Column("license_id", sa.String(36), nullable=False),
        sa.Column("created_at", sa.BigInteger, nullable=False))


def downgrade():
    for table in ["license_audit", "generation_jobs", "license_devices", "licenses"]:
        op.drop_table(table)
