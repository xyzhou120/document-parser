"""根据不含凭据的元数据生成两份空数据库初始化 SQL。

运行 python -m migrations.bootstrap --check 检测差异；审核模型或迁移变更后，
可以使用 --write 写入。本命令不会连接数据库。
"""

import argparse
from pathlib import Path

from sqlalchemy.dialects.postgresql import dialect
from sqlalchemy.schema import CreateIndex, CreateTable

from migrations.metadata import load_metadata

AUDIT_TABLES = (
    "field_review_revisions", "rule_bundles", "rule_release_events", "source_revisions",
    "content_revisions", "content_impacts", "content_invalidations", "assistant_feedback_samples",
)


def canonical_sql():
    metadata = load_metadata()
    output = [
        "-- 由 python -m migrations.bootstrap 生成；模型与 Alembic 变更需一并审核。",
        "-- 仅用于空数据库初始化。现有部署必须在隔离环境中比较数据模式，",
        "-- 并审核 Alembic 接管过程；IF NOT EXISTS 不会修复已有列或索引的差异。",
        "-- 此文件不代表已执行 alembic stamp；生成器不会连接线上数据库。",
        "BEGIN;",
    ]
    for table in metadata.sorted_tables:
        output.append(str(CreateTable(table, if_not_exists=True).compile(dialect=dialect())).strip() + ";")
        for index in sorted(table.indexes, key=lambda item: item.name):
            output.append(str(CreateIndex(index, if_not_exists=True).compile(dialect=dialect())).strip() + ";")
    output.append("""CREATE OR REPLACE FUNCTION reject_platform_audit_mutation()
RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN
RAISE EXCEPTION '% is append-only', TG_TABLE_NAME; END; $$;""")
    for table in AUDIT_TABLES:
        output.append(f"""DO $$ BEGIN
IF NOT EXISTS (SELECT 1 FROM pg_trigger
WHERE tgname = '{table}_immutable_audit' AND tgrelid = '{table}'::regclass) THEN
CREATE TRIGGER {table}_immutable_audit BEFORE UPDATE OR DELETE ON {table}
FOR EACH ROW EXECUTE FUNCTION reject_platform_audit_mutation();
END IF; END; $$;""")
    output.append("COMMIT;")
    return "\n\n".join(output) + "\n"


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--check", action="store_true")
    action.add_argument("--write", action="store_true")
    args = parser.parse_args(argv)
    root = Path(__file__).resolve().parents[2]
    paths = [root / "backend/migrations/init.sql",
             root / "deploy/dependencies/postgres/init/001_schema.sql"]
    expected = canonical_sql()
    if args.write:
        for path in paths:
            path.write_text(expected, encoding="utf-8")
        print(f"Wrote {len(paths)} schema files; {len(load_metadata().tables)} tables")
        return 0
    mismatched = [str(path.relative_to(root)) for path in paths
                  if not path.is_file() or path.read_text(encoding="utf-8") != expected]
    if mismatched:
        print("Bootstrap SQL drift: " + ", ".join(mismatched))
        return 1
    print(f"Canonical SQL consistent: {len(paths)} files, {len(load_metadata().tables)} tables")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
