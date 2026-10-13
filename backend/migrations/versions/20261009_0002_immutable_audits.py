"""防止直接使用 SQL 改写审计历史。"""

from alembic import op

revision = "20261009_0002"
down_revision = "20261009_0001"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""CREATE OR REPLACE FUNCTION reject_platform_audit_mutation()
        RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN
        RAISE EXCEPTION '% is append-only', TG_TABLE_NAME; END; $$""")
    for table in ("field_review_revisions", "rule_bundles", "rule_release_events", "source_revisions"):
        op.execute(f"""CREATE TRIGGER {table}_immutable_audit BEFORE UPDATE OR DELETE ON {table}
            FOR EACH ROW EXECUTE FUNCTION reject_platform_audit_mutation()""")


def downgrade():
    raise RuntimeError("Audit integrity downgrade is disabled; use an isolated restore")
