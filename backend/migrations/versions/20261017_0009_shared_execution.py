"""共享执行 owner/subscriber 身份；不改变历史运行归属。"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "20261017_0009"
down_revision = "20261016_0008"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "shared_executions",
        sa.Column("shared_execution_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("identity_hash", sa.String(length=64), nullable=False),
        sa.Column("file_hash", sa.String(length=64), nullable=False),
        sa.Column("content_digest", sa.String(length=64), nullable=True),
        sa.Column("coverage_digest", sa.String(length=64), nullable=True),
        sa.Column("preprocess_version", sa.String(length=120), nullable=False),
        sa.Column("contract_hash", sa.String(length=64), nullable=False),
        sa.Column("model_signature", sa.String(length=64), nullable=False),
        sa.Column("source_run_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("source_version_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("state", sa.String(length=32), nullable=False),
        sa.Column("execution_generation", sa.Integer(), nullable=False),
        sa.Column("fence_token", sa.Integer(), nullable=False),
        sa.Column("manifest", postgresql.JSON(astext_type=sa.Text()), nullable=False),
        sa.Column("failure_code", sa.String(length=200), nullable=True),
        sa.Column("lease_owner", sa.String(length=200), nullable=True),
        sa.Column("lease_until", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("completed_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["source_run_id"], ["parse_runs.parse_run_id"]),
        sa.ForeignKeyConstraint(["source_version_id"], ["attachment_versions.id"]),
        sa.PrimaryKeyConstraint("shared_execution_id"),
        sa.UniqueConstraint("identity_hash", name="uq_shared_execution_identity"),
    )
    op.create_index("ix_shared_executions_state", "shared_executions", ["state"], unique=False)
    op.create_index("ix_shared_executions_created_at", "shared_executions", ["created_at"], unique=False)
    op.create_index("ix_shared_executions_source_run_id", "shared_executions", ["source_run_id"], unique=False)
    op.create_index("ix_shared_executions_source_version_id", "shared_executions", ["source_version_id"], unique=False)
    op.create_table(
        "shared_execution_subscribers",
        sa.Column("subscriber_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("shared_execution_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("target_attachment_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("target_document_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("target_version_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("parse_run_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("expected_cas", sa.Integer(), nullable=False),
        sa.Column("expected_invalidation_watermark", sa.Integer(), nullable=True),
        sa.Column("requested_contract_hash", sa.String(length=64), nullable=False),
        sa.Column("state", sa.String(length=32), nullable=False),
        sa.Column("source_generation", sa.Integer(), nullable=False),
        sa.Column("failure_code", sa.String(length=200), nullable=True),
        sa.Column("provenance", postgresql.JSON(astext_type=sa.Text()), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("attached_at", sa.DateTime(), nullable=True),
        sa.Column("completed_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["shared_execution_id"], ["shared_executions.shared_execution_id"]),
        sa.ForeignKeyConstraint(["target_attachment_id"], ["attachments.id"]),
        sa.ForeignKeyConstraint(["target_document_id"], ["documents.document_id"]),
        sa.ForeignKeyConstraint(["target_version_id"], ["attachment_versions.id"]),
        sa.ForeignKeyConstraint(["parse_run_id"], ["parse_runs.parse_run_id"]),
        sa.PrimaryKeyConstraint("subscriber_id"),
        sa.UniqueConstraint("shared_execution_id", "parse_run_id", name="uq_shared_subscriber_run"),
        sa.UniqueConstraint("target_version_id", name="uq_shared_subscriber_version"),
    )
    op.create_index("ix_shared_execution_subscribers_state", "shared_execution_subscribers", ["state"], unique=False)
    op.create_index("ix_shared_execution_subscribers_shared_execution_id", "shared_execution_subscribers", ["shared_execution_id"], unique=False)
    op.create_index("ix_shared_execution_subscribers_target_attachment_id", "shared_execution_subscribers", ["target_attachment_id"], unique=False)
    op.create_index("ix_shared_execution_subscribers_target_document_id", "shared_execution_subscribers", ["target_document_id"], unique=False)
    op.create_index("ix_shared_execution_subscribers_parse_run_id", "shared_execution_subscribers", ["parse_run_id"], unique=False)
    op.create_index("ix_shared_execution_subscribers_created_at", "shared_execution_subscribers", ["created_at"], unique=False)


def downgrade():
    op.drop_table("shared_execution_subscribers")
    op.drop_table("shared_executions")
