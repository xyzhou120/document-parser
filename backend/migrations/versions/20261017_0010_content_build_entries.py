"""内容构建源输入与区域增量存储；不批量改写历史检查点。"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "20261017_0010"
down_revision = "20261017_0009"
branch_labels = None
depends_on = None


def upgrade():
    # 本地 init_db 可能已补表/补列；显式迁移仍需要正确前进版本号。
    op.execute("ALTER TABLE content_build_jobs ADD COLUMN IF NOT EXISTS source_pages JSON")
    if not op.get_context().as_sql and sa.inspect(op.get_bind()).has_table("content_build_entries"):
        return
    op.create_table("content_build_entries",
        sa.Column("build_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("entry_key", sa.String(500), nullable=False),
        sa.Column("payload", postgresql.JSON(), nullable=False),
        sa.ForeignKeyConstraint(["build_id"], ["content_build_jobs.build_id"]),
        sa.PrimaryKeyConstraint("build_id", "kind", "entry_key"))


def downgrade():
    # 退回旧格式必须先把区域回填到 checkpoint；禁止静默丢弃已付费结果。
    raise RuntimeError("content checkpoint v2 requires an explicit backfill before downgrade")
