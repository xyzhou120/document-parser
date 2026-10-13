#!/usr/bin/env python3
"""创建空开发数据库，并且只复制清单知识包。

复制内容（知识数据）：checklist_types、rule_bundles、extraction_rules、rule_release_events、
checklist_release_heads、rule_change_log（规则编辑历史）。

不复制的内容（测试记录，保留在源数据库）：documents、parse_runs、extracted_fields、
document_artifacts、parse_logs、batches、field_review_revisions、integration 和 context 表。

源数据库只通过只读会话读取。新数据库的数据模式来自 migrations/init.sql，
该文件由模型生成，并通过 python -m migrations.bootstrap --check 检查。
不使用 Alembic：源数据库没有 alembic_version 表，因此从未标记迁移版本。

在 backend 目录执行：
    python3 scripts/dev_db/clone_knowledge_pack.py --target-db document_parser_dev --dry-run
    python3 scripts/dev_db/clone_knowledge_pack.py --target-db document_parser_dev
连接其他服务器时，增加 --target-host、--target-port、--target-user，并在环境中设置
TARGET_DB_PASSWORD（不会打印密码）。目标数据库必须尚不存在。
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path
from urllib.parse import quote_plus

BACKEND = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(BACKEND))

KNOWLEDGE_TABLES = [
    "checklist_types",
    "rule_bundles",  # 放在引用它的两个表之前。
    "extraction_rules",
    "rule_release_events",
    "checklist_release_heads",
    "rule_change_log",
]
SAFE_NAME = re.compile(r"^[A-Za-z0-9_]{1,63}$")


def database_url(host: str, port: int, user: str, password: str, name: str) -> str:
    return (f"postgresql+psycopg2://{quote_plus(user)}:{quote_plus(password)}"
            f"@{host}:{port}/{quote_plus(name)}")


def make_engine(url: str, *, read_only: bool = False, autocommit: bool = False):
    from sqlalchemy import create_engine

    connect_args = {"connect_timeout": 10}
    if read_only:
        connect_args["options"] = "-c default_transaction_read_only=on"
    kwargs = {"connect_args": connect_args}
    if autocommit:
        kwargs["isolation_level"] = "AUTOCOMMIT"
    return create_engine(url, **kwargs)


def fingerprint(conn, table: str):
    """返回行数和不依赖列顺序或 JSON 空白字符的内容哈希。"""
    from sqlalchemy import text

    row = conn.execute(text(
        f"select count(*), md5(coalesce(string_agg(j, '|' order by j), '')) "
        f"from (select row_to_json(x)::jsonb::text as j from {table} x) s"
    )).one()
    return int(row[0]), row[1]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--target-db", required=True)
    parser.add_argument("--target-host")
    parser.add_argument("--target-port", type=int)
    parser.add_argument("--target-user")
    parser.add_argument("--dry-run", action="store_true", help="read the source and print the plan only")
    args = parser.parse_args(argv)
    if not SAFE_NAME.match(args.target_db):
        raise SystemExit("target database name must be letters, digits and underscores")

    import psycopg2.extras
    from sqlalchemy import inspect, text

    from app.config import settings
    from migrations.metadata import load_metadata

    psycopg2.extras.register_uuid()  # UUID 列返回模型预期的 uuid.UUID。
    src = settings.database
    tgt_host = args.target_host or src.host
    tgt_port = args.target_port or src.port
    tgt_user = args.target_user or src.user
    if args.target_host and args.target_host != src.host:
        tgt_password = os.environ.get("TARGET_DB_PASSWORD", "")
        if not tgt_password:
            raise SystemExit("set TARGET_DB_PASSWORD for a different server (not printed)")
    else:
        tgt_password = os.environ.get("TARGET_DB_PASSWORD") or src.password
    if tgt_host == src.host and tgt_port == src.port and args.target_db == src.name:
        raise SystemExit("the target must be a different database from the source")

    metadata = load_metadata()
    source = make_engine(database_url(src.host, src.port, src.user, src.password, src.name), read_only=True)
    rows_by_table: dict[str, list[dict]] = {}
    source_fingerprint: dict[str, tuple] = {}
    with source.connect() as conn:
        for table in KNOWLEDGE_TABLES:
            rows_by_table[table] = [dict(r._mapping) for r in conn.execute(text(f"select * from {table}"))]
            source_fingerprint[table] = fingerprint(conn, table)
    print(f"source {src.name} @ {src.host}: knowledge rows")
    for table in KNOWLEDGE_TABLES:
        print(f"  {table:24s} {source_fingerprint[table][0]}")
    print(f"target {args.target_db} @ {tgt_host}:{tgt_port}  (will be created; not copied from anywhere else)")
    if args.dry_run:
        print("dry run: nothing created, nothing written")
        return 0

    admin = make_engine(database_url(tgt_host, tgt_port, tgt_user, tgt_password, "postgres"), autocommit=True)
    with admin.connect() as conn:
        exists = conn.execute(text("select 1 from pg_database where datname = :n"),
                              {"n": args.target_db}).scalar()
        if exists:
            raise SystemExit(f"database {args.target_db} already exists; choose another name")
        conn.execute(text(f'CREATE DATABASE "{args.target_db}"'))
    print(f"created {args.target_db}")

    target = make_engine(database_url(tgt_host, tgt_port, tgt_user, tgt_password, args.target_db))
    schema_sql = (BACKEND / "migrations" / "init.sql").read_text(encoding="utf-8")
    raw = target.raw_connection()
    try:
        cursor = raw.cursor()
        cursor.execute(schema_sql)
        raw.commit()
    finally:
        raw.close()
    with target.connect() as conn:
        tables = set(inspect(conn).get_table_names())
    print(f"schema applied: {len(tables)} tables (models declare {len(metadata.tables)})")
    if tables != set(metadata.tables):
        raise SystemExit("schema mismatch after init.sql; stop and inspect")

    with target.begin() as conn:
        for table in KNOWLEDGE_TABLES:
            rows = rows_by_table[table]
            if rows:
                conn.execute(metadata.tables[table].insert(), rows)
    print("knowledge rows copied")

    failed = []
    with target.connect() as conn:
        for table in KNOWLEDGE_TABLES:
            got = fingerprint(conn, table)
            ok = got == source_fingerprint[table]
            print(f"  verify {table:24s} {'OK' if ok else 'MISMATCH'} rows={got[0]}")
            if not ok:
                failed.append(table)
    if failed:
        print("verification failed for:", ", ".join(failed))
        return 1
    print(f"done. To use it, set database.name: {args.target_db} in config/local.yaml (keep a backup of the "
          f"original file) and restart API, worker and web.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
