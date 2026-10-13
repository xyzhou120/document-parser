"""在冻结基线之后增加持久化集成收件箱和发件箱表。"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql
from sqlalchemy.sql.sqltypes import Text

revision = "20261009_0003"
down_revision = "20261009_0002"
branch_labels = None
depends_on = None


def upgrade():
    # ### Alembic 自动生成的命令，请按需调整！###
    op.create_table(
        "integration_requests",
        sa.Column("event_id", sa.String(length=160), nullable=False),
        sa.Column("document_id", sa.UUID(), nullable=False),
        sa.Column("parse_run_id", sa.UUID(), nullable=False),
        sa.Column("payload_hash", sa.String(length=64), nullable=False),
        sa.Column("payload", postgresql.JSON(astext_type=Text()), nullable=False),
        sa.Column("state", sa.String(length=30), nullable=False),
        sa.Column("actor", sa.String(length=200), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("event_id"),
    )
    op.create_index(
        op.f("ix_integration_requests_document_id"),
        "integration_requests",
        ["document_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_integration_requests_parse_run_id"),
        "integration_requests",
        ["parse_run_id"],
        unique=False,
    )
    op.create_table(
        "integration_outbox",
        sa.Column("event_id", sa.String(length=160), nullable=False),
        sa.Column("parse_run_id", sa.UUID(), nullable=False),
        sa.Column("document_id", sa.UUID(), nullable=False),
        sa.Column("payload", postgresql.JSON(astext_type=Text()), nullable=False),
        sa.Column("destination", sa.String(length=1000), nullable=False),
        sa.Column("state", sa.String(length=30), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("max_attempts", sa.Integer(), nullable=False),
        sa.Column("next_attempt_at", sa.DateTime(), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("acknowledgement", postgresql.JSON(astext_type=Text()), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("event_id"),
    )
    op.create_index(
        op.f("ix_integration_outbox_document_id"),
        "integration_outbox",
        ["document_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_integration_outbox_parse_run_id"),
        "integration_outbox",
        ["parse_run_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_integration_outbox_state"), "integration_outbox", ["state"], unique=False
    )
    # ### Alembic 命令结束 ###


def downgrade():
    raise RuntimeError("Integration history downgrade is disabled; restore an isolated backup")
