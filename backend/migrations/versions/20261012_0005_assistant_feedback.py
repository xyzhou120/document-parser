"""将离线反馈与来源及人工事实审计历史分离。"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "20261012_0005"
down_revision = "20261012_0004"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "assistant_feedback_samples",
        sa.Column("feedback_id", sa.UUID(), nullable=False),
        sa.Column("document_id", sa.UUID(), nullable=False),
        sa.Column("request_id", sa.String(200), nullable=False),
        sa.Column("qa_message_id", sa.UUID(), nullable=True),
        sa.Column("parse_run_id", sa.UUID(), nullable=True),
        sa.Column("content_revision_id", sa.UUID(), nullable=False),
        sa.Column("generation_id", sa.UUID(), nullable=True),
        sa.Column("sample_type", sa.String(40), nullable=False),
        sa.Column("adjudication_status", sa.String(30), nullable=False, server_default="pending"),
        sa.Column("submitted_by", sa.String(200), nullable=False),
        sa.Column("snapshot", postgresql.JSON(), nullable=False),
        sa.Column("expected_correction", postgresql.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("feedback_id"),
        sa.UniqueConstraint("document_id", "request_id", name="uq_assistant_feedback_document_request"),
        sa.CheckConstraint("sample_type IN ('qa','extraction')", name="ck_assistant_feedback_type"),
        sa.CheckConstraint("adjudication_status = 'pending'", name="ck_assistant_feedback_pending"),
        sa.CheckConstraint("qa_message_id IS NOT NULL OR parse_run_id IS NOT NULL",
                           name="ck_assistant_feedback_origin"),
        sa.ForeignKeyConstraint(["document_id"], ["documents.document_id"]),
        sa.ForeignKeyConstraint(["qa_message_id"], ["qa_messages.message_id"]),
        sa.ForeignKeyConstraint(["parse_run_id"], ["parse_runs.parse_run_id"]),
        sa.ForeignKeyConstraint(["content_revision_id"], ["content_revisions.revision_id"]),
        sa.ForeignKeyConstraint(["generation_id"], ["index_generations.generation_id"]),
    )
    for column in ("document_id", "qa_message_id", "parse_run_id", "content_revision_id", "generation_id", "created_at"):
        op.create_index(f"ix_assistant_feedback_samples_{column}", "assistant_feedback_samples", [column])
    op.execute("""CREATE TRIGGER assistant_feedback_samples_immutable_audit
BEFORE UPDATE OR DELETE ON assistant_feedback_samples
FOR EACH ROW EXECUTE FUNCTION reject_platform_audit_mutation()""")


def downgrade():
    raise RuntimeError("Feedback history downgrade is disabled; restore an isolated backup")
