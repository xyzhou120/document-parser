"""Verify the v2 migration in a new isolated PostgreSQL database.

Never uses the configured business database as a migration target.  The default
connection only reaches the postgres maintenance database to create a uniquely
named empty verification database.  The database is retained for inspection;
this script never drops a database or changes application configuration.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import subprocess
import sys
import uuid
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import quote

import asyncpg
import yaml

BACKEND = Path(__file__).resolve().parents[1]
ROOT = BACKEND.parent
BASELINE = "20261009_0003"


def configuration() -> dict:
    """Read connection fields without logging secrets or loading app services."""
    merged: dict = {}
    for name in ("default.yaml", "local.yaml"):
        path = ROOT / "config" / name
        if path.exists():
            values = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
            merged.update(values.get("database") or {})
    return merged


def run_alembic(database_url: str, *arguments: str) -> str:
    env = dict(os.environ, PLATFORM_MIGRATION_DATABASE_URL=database_url)
    command = [sys.executable, "-m", "alembic", "-c", str(BACKEND / "alembic.ini"), *arguments]
    result = subprocess.run(command, cwd=BACKEND, env=env, text=True, capture_output=True)
    if result.returncode:
        # Never print exception text that could contain a connection URL.
        raise RuntimeError(f"Alembic {' '.join(arguments)} failed; exit={result.returncode}")
    return (result.stdout + result.stderr).strip()


async def verify(args: argparse.Namespace) -> dict:
    config = configuration()
    database = args.database
    if not re.fullmatch(r"document_parser_v2_g1_[0-9]{8}_[a-f0-9]{8}", database):
        raise ValueError("Target must be a unique document_parser_v2_g1_DATE_RANDOM database")
    if database == config.get("name") or database == "postgres":
        raise ValueError("The business or maintenance database cannot be a migration target")
    connection_options = {
        "host": config.get("host", "localhost"),
        "port": int(config.get("port", 5432)),
        "user": config.get("user", "postgres"),
        "password": config.get("password", ""),
        "timeout": 15,
    }
    admin = await asyncpg.connect(database="postgres", **connection_options)
    try:
        if await admin.fetchval("SELECT 1 FROM pg_database WHERE datname = $1", database):
            raise ValueError("Verification database already exists; choose a new target")
        await admin.execute(f'CREATE DATABASE "{database}"')
    finally:
        await admin.close()

    url = (
        "postgresql+asyncpg://"
        + quote(str(connection_options["user"]), safe="")
        + ":" + quote(str(connection_options["password"]), safe="")
        + "@" + str(connection_options["host"])
        + ":" + str(connection_options["port"])
        + "/" + database
    )
    report: dict = {
        "database": database,
        "created_at": datetime.now(UTC).isoformat(),
        "business_database_accessed": False,
        "database_retained": True,
        "checks": [],
    }
    # Run Alembic outside this loop's connection lifetime; migration uses its own loop.
    await asyncio.to_thread(run_alembic, url, "upgrade", BASELINE)
    report["checks"].append("baseline_upgrade")
    document_id, artifact_id, run_id, source_id = (uuid.uuid4() for _ in range(4))
    conn = await asyncpg.connect(database=database, **connection_options)
    try:
        await conn.execute(
            """INSERT INTO documents
            (document_id,file_name,original_name,file_path,file_size,file_hash,mime_type,
             parse_status,created_at,updated_at)
            VALUES($1,'g1.pdf','g1.pdf','verification/g1.pdf',1,$2,'application/pdf',
                   'COMPLETED',CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)""",
            document_id, "a" * 64,
        )
        await conn.execute(
            """INSERT INTO document_artifacts
            (artifact_id,document_id,file_hash,preprocess_profile_version,status,pages,quality,
             created_at,updated_at)
            VALUES($1,$2,$3,'legacy-g1','READY','[]'::json,'{}'::json,
                   CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)""",
            artifact_id, document_id, "a" * 64,
        )
        await conn.execute(
            """INSERT INTO parse_runs
            (parse_run_id,document_id,artifact_id,status,fields_extracted,created_at)
            VALUES($1,$2,$3,'COMPLETED',0,CURRENT_TIMESTAMP)""",
            run_id, document_id, artifact_id,
        )
        await conn.execute(
            """INSERT INTO source_revisions
            (revision_id,document_id,parent_artifact_id,revision_type,page_number,comment,
             affected_fields,source_review_status,created_by,created_at)
            VALUES($1,$2,$3,'CONFIRM',1,'g1 synthetic fixture','[]'::json,
                   'PARTIALLY_REVIEWED','migration-verifier',CURRENT_TIMESTAMP)""",
            source_id, document_id, artifact_id,
        )
    finally:
        await conn.close()
    report["checks"].append("synthetic_legacy_rows_seeded")

    await asyncio.to_thread(run_alembic, url, "upgrade", "head")
    report["checks"].append("head_upgrade")
    conn = await asyncpg.connect(database=database, **connection_options)
    try:
        row = await conn.fetchrow(
            "SELECT status,content_revision_id,contract_version,execution_generation "
            "FROM parse_runs WHERE parse_run_id=$1", run_id,
        )
        if row is None or row["status"] != "COMPLETED" or row["content_revision_id"] is not None:
            raise AssertionError("Legacy ParseRun was lost or given fabricated coverage")
        report["checks"].append("legacy_rows_preserved")
        report["legacy_generation"] = row["execution_generation"]
        try:
            await conn.execute(
                "UPDATE source_revisions SET comment='must fail' WHERE revision_id=$1",
                source_id,
            )
        except asyncpg.PostgresError:
            report["checks"].append("source_audit_remains_immutable")
        else:
            raise AssertionError("Append-only SourceRevision guard no longer works")
        report["alembic_revision"] = await conn.fetchval("SELECT version_num FROM alembic_version")
        report["table_count"] = await conn.fetchval(
            "SELECT count(*) FROM information_schema.tables WHERE table_schema='public'"
        )
    finally:
        await conn.close()
    # Compare Alembic autogenerate against the complete model metadata.
    await asyncio.to_thread(run_alembic, url, "check")
    report["checks"].append("alembic_metadata_no_drift")
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", required=True)
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    try:
        report = asyncio.run(verify(args))
    except Exception as exc:
        # Connection exceptions sometimes include credentials. Keep output minimal.
        print(f"Isolated G1 verification failed: {type(exc).__name__}", file=sys.stderr)
        return 1
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

