"""为规则增加一等提取目标契约字段。"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql
from sqlalchemy.sql.sqltypes import Text

revision = "20261014_0006"
down_revision = "20261012_0005"
branch_labels = None
depends_on = None

JSON = postgresql.JSON(astext_type=Text())


def upgrade():
    op.add_column("extraction_rules", sa.Column("target_type", sa.String(30), nullable=False, server_default="fact"))
    op.add_column("extraction_rules", sa.Column("target_group_key", sa.String(100), nullable=True))
    op.add_column("extraction_rules", sa.Column("target_group_name", sa.String(255), nullable=True))
    json_default = sa.text("'{}'::json")
    op.add_column("extraction_rules", sa.Column("output_schema", JSON, nullable=False, server_default=json_default))
    op.add_column("extraction_rules", sa.Column("source_policy", JSON, nullable=False, server_default=json_default))
    op.add_column("extraction_rules", sa.Column("evidence_policy", JSON, nullable=False, server_default=json_default))
    op.create_index("ix_extraction_rules_target_group_key", "extraction_rules", ["target_group_key"])
    op.add_column("extracted_fields", sa.Column("target_type", sa.String(30), nullable=True))
    op.add_column("extracted_fields", sa.Column("target_group_key", sa.String(100), nullable=True))
    op.add_column("extracted_fields", sa.Column("target_status", sa.String(40), nullable=True))
    op.add_column("extracted_fields", sa.Column("status_reason", JSON, nullable=True))
    op.create_index("ix_extracted_fields_target_group_key", "extracted_fields", ["target_group_key"])
    op.create_index("ix_extracted_fields_target_status", "extracted_fields", ["target_status"])


def downgrade():
    op.drop_index("ix_extraction_rules_target_group_key", table_name="extraction_rules")
    op.drop_index("ix_extracted_fields_target_status", table_name="extracted_fields")
    op.drop_index("ix_extracted_fields_target_group_key", table_name="extracted_fields")
    for column in ("status_reason", "target_status", "target_group_key", "target_type"):
        op.drop_column("extracted_fields", column)
    for column in ("evidence_policy", "source_policy", "output_schema", "target_group_name", "target_group_key", "target_type"):
        op.drop_column("extraction_rules", column)
