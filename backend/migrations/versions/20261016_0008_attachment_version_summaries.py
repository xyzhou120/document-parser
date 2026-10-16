"""列表读取用的附件版本摘要，避免查询拉出完整 snapshot。"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "20261016_0008"
down_revision = "20261015_0007"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "attachment_version_summaries",
        sa.Column("version_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("summary", postgresql.JSON(astext_type=sa.Text()), nullable=False),
        sa.ForeignKeyConstraint(["version_id"], ["attachment_versions.id"]),
        sa.PrimaryKeyConstraint("version_id"),
    )
    op.execute("""
        INSERT INTO attachment_version_summaries (version_id, summary)
        SELECT v.id, json_build_object(
            'stats', v.snapshot->'stats',
            'duration_seconds', v.snapshot->'duration_seconds',
            'cumulative_duration_seconds', v.snapshot->'cumulative_duration_seconds',
            'content_revision_id', v.snapshot->'content_revision_id',
            'invalidation_watermark', v.snapshot->'invalidation_watermark'
        )
        FROM attachment_versions v
        WHERE v.snapshot IS NOT NULL
          AND NOT EXISTS (
            SELECT 1 FROM attachment_version_summaries s WHERE s.version_id = v.id
          )
    """)


    op.add_column("extracted_fields", sa.Column("quality_inputs", postgresql.JSON(astext_type=sa.Text()), nullable=True))
    op.execute("""
        UPDATE extracted_fields
        SET quality_inputs = json_strip_nulls(json_build_object(
            'contract_version', extraction_detail->'contract_version',
            'semantic_state', extraction_detail->'semantic_state',
            'coverage_complete', extraction_detail->'coverage_complete',
            'confidence', extraction_detail->'confidence',
            'coverage', extraction_detail->'coverage',
            'failures', extraction_detail->'failures',
            'value_groups', extraction_detail->'value_groups',
            'instances', extraction_detail->'instances',
            'field_completion', CASE WHEN extraction_detail->'field_completion' IS NULL THEN NULL
                ELSE json_build_object(
                  'semantic_state', extraction_detail->'field_completion'->'semantic_state',
                  'coverage_complete', extraction_detail->'field_completion'->'coverage_complete') END,
            'completion', CASE WHEN extraction_detail->'completion' IS NULL THEN NULL
                ELSE json_build_object(
                  'semantic_state', extraction_detail->'completion'->'semantic_state',
                  'coverage_complete', extraction_detail->'completion'->'coverage_complete') END
        ))
        WHERE quality_inputs IS NULL AND extraction_detail IS NOT NULL
    """)


def downgrade():
    op.drop_column("extracted_fields", "quality_inputs")
    op.drop_table("attachment_version_summaries")
