"""Local operational reports and immutable backup handoff packages.

No command connects or changes a live service by default. Backup packages contain
an operator-produced PostgreSQL custom dump and a logical MinIO object export,
never a copy of running database/object-store data directories.
"""

import argparse
import asyncio
import hashlib
import json
import math
import shutil
import statistics
import sys
import time
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from urllib.parse import urlparse
from urllib.request import HTTPRedirectHandler, build_opener

import yaml

if str(Path(__file__).resolve().parents[1]) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def sha256(path):
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def safe_member(root, name):
    if not isinstance(name, str) or not name:
        raise ValueError("Unsafe package member")
    member = PurePosixPath(name)
    if (
        not isinstance(name, str)
        or not name
        or member.is_absolute()
        or ".." in member.parts
        or "\\" in name
        or ":" in name
    ):
        raise ValueError("Unsafe package member")
    target = root.joinpath(*member.parts)
    if not target.resolve().is_relative_to(root.resolve()):
        raise ValueError("Package member escapes root")
    for path in (target, *target.parents):
        if path.is_symlink():
            raise ValueError("Symbolic links are not supported")
        if path == root:
            break
    return target


def require_new_target(target, sources=()):
    target = target.resolve()
    if target.exists():
        raise ValueError("Target must not exist; overwriting is forbidden")
    for source in sources:
        source = source.resolve()
        if target.is_relative_to(source) or source.is_relative_to(target):
            raise ValueError("Target and source must not overlap")
    return target


def create_backup(database_dump, objects, target, consistency_note, dry_run=True):
    database_dump, objects = Path(database_dump), Path(objects)
    if not database_dump.is_file() or database_dump.is_symlink():
        raise ValueError("Provide a regular pg_dump custom-format file")
    with database_dump.open("rb") as handle:
        if handle.read(5) != b"PGDMP":
            raise ValueError("Database dump must use pg_dump --format=custom")
    if not objects.is_dir() or objects.is_symlink():
        raise ValueError("Provide a logical MinIO export directory")
    if not consistency_note.strip():
        raise ValueError("Record the write-freeze/export consistency procedure")
    files = sorted(path for path in objects.rglob("*") if path.is_file())
    for path in objects.rglob("*"):
        if path.is_symlink():
            raise ValueError("Object export must not contain symbolic links")
    if not files:
        raise ValueError("Object export is empty; explicit evidence is required")
    target = require_new_target(Path(target), [database_dump, objects])
    entries = [(database_dump, "database.dump", "postgresql_custom_dump")]
    entries.extend(
        (p, "objects/" + p.relative_to(objects).as_posix(), "minio_object") for p in files
    )
    manifest = {
        "schema_version": 1,
        "created_at": datetime.now(UTC).isoformat(),
        "consistency_note": consistency_note,
        "object_count": len(files),
        "files": [
            {"path": name, "kind": kind, "bytes": source.stat().st_size, "sha256": sha256(source)}
            for source, name, kind in entries
        ],
    }
    if dry_run:
        return {"dry_run": True, "manifest": manifest}
    target.mkdir(parents=True)
    for source, name, _kind in entries:
        destination = safe_member(target, name)
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, destination)
    # Recheck copied bytes; never label a package valid following concurrent change.
    for item in manifest["files"]:
        destination = safe_member(target, item["path"])
        if destination.stat().st_size != item["bytes"] or sha256(destination) != item["sha256"]:
            raise ValueError("Source changed during packaging; discard incomplete package")
    write_json(target / "manifest.json", manifest)
    return {"dry_run": False, "verified": verify_backup(target)}


def verify_backup(package):
    package = Path(package).resolve()
    manifest_path = safe_member(package, "manifest.json")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("schema_version") != 1 or not isinstance(manifest.get("files"), list):
        raise ValueError("Unsupported or incomplete manifest")
    expected = {"manifest.json"}
    database_count, object_count = 0, 0
    for item in manifest["files"]:
        name = item.get("path", "")
        path = safe_member(package, name)
        if name in expected:
            raise ValueError("Duplicate package member")
        expected.add(name)
        if not path.is_file():
            raise ValueError("Missing package member or byte count mismatch")
        if sha256(path) != item.get("sha256"):
            raise ValueError("SHA256 mismatch")
        if path.stat().st_size != item.get("bytes"):
            raise ValueError("Missing package member or byte count mismatch")
        if name == "database.dump" and item.get("kind") == "postgresql_custom_dump":
            with path.open("rb") as handle:
                if handle.read(5) != b"PGDMP":
                    raise ValueError("Invalid database dump signature")
            database_count += 1
        elif name.startswith("objects/") and item.get("kind") == "minio_object":
            object_count += 1
        else:
            raise ValueError("Unknown package member kind")
    actual = set()
    for path in package.rglob("*"):
        if path.is_symlink():
            raise ValueError("Package contains symbolic links")
        if path.is_file():
            actual.add(path.relative_to(package).as_posix())
    if actual != expected or database_count != 1 or object_count < 1:
        raise ValueError("Unexpected files or missing database/object export")
    if object_count != manifest.get("object_count") or not manifest.get("consistency_note"):
        raise ValueError("Missing consistency record or object count mismatch")
    return {
        "valid": True,
        "files": len(expected) - 1,
        "objects": object_count,
        "integrity_only": True,
    }


def restore_plan(package, target, database_name, bucket, execute=False):
    verify_backup(package)
    if not database_name or not bucket:
        raise ValueError("Explicit isolated database name and bucket are required")
    # Do not accept URLs/passwords/commands as target identifiers.
    import re

    if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,62}", database_name):
        raise ValueError("Use a plain isolated database name")
    if not re.fullmatch(r"[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]", bucket):
        raise ValueError("Use a plain isolated bucket name")
    if "restore" not in database_name.lower() or "restore" not in bucket:
        raise ValueError("Isolated database and bucket names must include restore")
    package = Path(package).resolve()
    target = require_new_target(Path(target), [package])
    report = {
        "dry_run": not execute,
        "target_database": database_name,
        "target_bucket": bucket,
        "live_restore_performed": False,
        "steps": [
            "Create a NEW empty isolated database and NEW private bucket",
            "Use pg_restore --exit-on-error --no-owner (no --clean) with a credential-safe service file",
            "Upload staged objects preserving keys to the NEW bucket; compare every SHA256",
            "Check schema with alembic check; run API, artifact and evidence smoke checks",
            "Record RTO, object counts, row counts and restore result before any traffic switch",
        ],
    }
    if execute:
        # Only stage verified local files. Never connect to or overwrite live DB/storage.
        target.mkdir(parents=True)
        shutil.copytree(package, target / "package")
        verify_backup(target / "package")
        write_json(target / "restore-plan.json", report)
    return report


def summarize_metrics(snapshot, max_queue=100, max_failure_rate=0.1):
    runs = snapshot.get("runs", [])

    def samples(items):
        return [
            float(v)
            for v in items
            if isinstance(v, int | float)
            and not isinstance(v, bool)
            and math.isfinite(v)
            and v >= 0
        ]

    def summary(values):
        values = sorted(values)
        return {
            "count": len(values),
            "p50_seconds": statistics.median(values) if values else None,
            "p95_seconds": values[max(0, math.ceil(len(values) * 0.95) - 1)] if values else None,
            "max_seconds": max(values) if values else None,
        }

    completed = [r for r in runs if r.get("status") in {"COMPLETED", "SUCCESS", "FAILED"}]
    failures = sum(r.get("status") == "FAILED" for r in completed)
    failure_rate = failures / len(completed) if completed else None
    durations = samples(r.get("duration_seconds") for r in runs)
    stages = {}
    for entry in snapshot.get("stage_logs", []):
        value = samples([entry.get("duration_seconds")])
        if value:
            stages.setdefault(entry.get("stage", "unknown"), []).extend(value)
    queue = snapshot.get("queue_depth")
    if not isinstance(queue, int) or isinstance(queue, bool) or queue < 0:
        queue = None
    alerts = []
    if queue is not None and queue > max_queue:
        alerts.append("QUEUE_DEPTH_EXCEEDED")
    if failure_rate is not None and failure_rate > max_failure_rate:
        alerts.append("FAILURE_RATE_EXCEEDED")
    if snapshot.get("healthy") is False:
        alerts.append("DEPENDENCY_UNHEALTHY")
    return {
        "observed_runs": len(runs),
        "terminal_runs": len(completed),
        "failure_rate": failure_rate,
        "queue_depth": queue,
        "durations": summary(durations),
        "stages": {k: summary(v) for k, v in stages.items()},
        "alerts": alerts,
        "scope": "supplied observation window; not a capacity benchmark",
    }


def local_url(url):
    parsed = urlparse(url)
    if (
        parsed.scheme not in {"http", "https"}
        or parsed.hostname not in {"localhost", "127.0.0.1", "::1"}
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("Only explicit loopback HTTP endpoints without credentials are allowed")
    return url.rstrip("/")


class LocalRedirectHandler(HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, msg, headers, newurl):
        local_url(newurl.split("?")[0])
        return super().redirect_request(request, fp, code, msg, headers, newurl)


async def dependency_check(execute=False):
    """Read-only local DB SELECT 1, Redis PING/LLEN and MinIO bucket HEAD."""
    names = ["postgresql", "redis", "minio"]
    if not execute:
        return {"dry_run": True, "checks": names, "queue_depth": None}
    from minio import Minio
    from redis.asyncio import Redis
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine
    from sqlalchemy.pool import NullPool
    from urllib3 import PoolManager, Timeout

    from app.config import settings

    def loopback(host):
        if host not in {"localhost", "127.0.0.1", "::1"}:
            raise ValueError("Dependency diagnostics support local targets only")

    async def database():
        loopback(settings.database.host)
        engine = create_async_engine(settings.database.url, poolclass=NullPool, echo=False)
        try:
            async with engine.connect() as connection:
                await connection.execute(text("SELECT 1"))
        finally:
            await engine.dispose()
        return {}

    async def redis():
        parsed = urlparse(settings.celery.broker_url)
        loopback(parsed.hostname)
        if parsed.scheme not in {"redis", "rediss"}:
            raise ValueError("Only Redis brokers are supported")
        client = Redis.from_url(
            settings.celery.broker_url, socket_timeout=5, socket_connect_timeout=5
        )
        try:
            await client.ping()
            queue_depth = await client.llen(settings.celery.task_default_queue)
        finally:
            await client.aclose()
        return {
            "queue_depth": queue_depth,
            "queue_scope": "default Redis list only; excludes active/reserved/priority queues",
        }

    async def minio():
        parsed = urlparse("https://" + settings.minio.endpoint)
        loopback(parsed.hostname)
        client = Minio(
            settings.minio.endpoint,
            access_key=settings.minio.access_key,
            secret_key=settings.minio.secret_key,
            secure=settings.minio.secure,
            http_client=PoolManager(timeout=Timeout(connect=3, read=5), retries=0),
        )
        exists = await asyncio.to_thread(client.bucket_exists, settings.minio.bucket)
        if not exists:
            raise ValueError("Configured bucket is missing")
        return {}

    async def checked(name, probe):
        started = time.monotonic()
        try:
            details = await asyncio.wait_for(probe(), timeout=10)
            return {
                "key": name,
                "status": "online",
                "latency_ms": round((time.monotonic() - started) * 1000, 1),
                **details,
            }
        except Exception:
            return {"key": name, "status": "unavailable", "latency_ms": None}

    checks = await asyncio.gather(
        checked(names[0], database), checked(names[1], redis), checked(names[2], minio)
    )
    return {
        "dry_run": False,
        "checks": checks,
        "healthy": all(c["status"] == "online" for c in checks),
        "queue_depth": next((c.get("queue_depth") for c in checks if c["key"] == "redis"), None),
        "scope": "local dependency reachability only; worker/model health not inferred",
    }


def observe(base_url, execute=False):
    base_url = local_url(base_url)
    paths = ["/api/health", "/api/stats/recent-runs?limit=50", "/api/stats/stage-duration"]
    if not execute:
        return {
            "dry_run": True,
            "requests": paths,
            "queue_depth": None,
            "dependency_health": "not probed",
        }
    data, checks = {}, []
    for path in paths:
        try:
            # Refuse redirects outside the explicitly authorized local host.
            with build_opener(LocalRedirectHandler).open(base_url + path, timeout=15) as response:
                local_url(response.geturl().split("?")[0])
                data[path] = json.load(response)
            checks.append({"path": path.split("?")[0], "status": "online"})
        except Exception:
            # Exception strings/response bodies may expose secrets. Report classification only.
            checks.append({"path": path.split("?")[0], "status": "unavailable"})
    snapshot = {
        "runs": data.get(paths[1], {}).get("items", []),
        "healthy": all(c["status"] == "online" for c in checks),
    }
    return {
        "dry_run": False,
        "checks": checks,
        "metrics": summarize_metrics(snapshot),
        "stage_aggregates": data.get(paths[2], {}),
        "dependency_health": "API reachability only; run dependency-check separately",
    }


def config_check(root):
    import re

    root = Path(root)
    config = yaml.safe_load((root / "config/default.yaml").read_text(encoding="utf-8"))
    max_file = min(config["upload"]["max_file_size"], 100 * 1024 * 1024)
    nginx = (root / "frontend/nginx.conf").read_text(encoding="utf-8")
    match = re.search(r"client_max_body_size\s+(\d+)([kKmM]?)\s*;", nginx)
    proxy = int(match[1]) * {"": 1, "k": 1024, "m": 1024**2}[match[2].lower()] if match else 0
    compose = yaml.safe_load(
        (root / "deploy/dependencies/docker-compose.yml").read_text(encoding="utf-8")
    )
    services = compose.get("services", {})
    checks = [
        {
            "key": "upload_multipart_headroom",
            "ok": proxy >= max_file + 1024 * 1024,
            "api_bytes": max_file,
            "proxy_bytes": proxy,
        },
        {"key": "dependency_services", "ok": {"postgres", "redis", "minio"} <= services.keys()},
        {
            "key": "database_volume",
            "ok": any(
                "/var/lib/postgresql/data" in v
                for v in services.get("postgres", {}).get("volumes", [])
            ),
        },
        {
            "key": "object_volume",
            "ok": any(v.endswith(":/data") for v in services.get("minio", {}).get("volumes", [])),
        },
    ]
    return {
        "valid": all(c["ok"] for c in checks),
        "checks": checks,
        "scope": "repository defaults; production environment overrides must be checked separately",
    }


def write_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    config = commands.add_parser("config-check")
    config.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[2])
    metrics = commands.add_parser("metrics")
    metrics.add_argument("--snapshot", type=Path, required=True)
    metrics.add_argument("--max-queue", type=int, default=100)
    metrics.add_argument("--max-failure-rate", type=float, default=0.1)
    monitor = commands.add_parser("observe")
    monitor.add_argument("--base-url", required=True)
    monitor.add_argument("--execute", action="store_true")
    dependency = commands.add_parser("dependency-check")
    dependency.add_argument("--execute", action="store_true")
    backup = commands.add_parser("backup")
    backup.add_argument("--database-dump", type=Path, required=True)
    backup.add_argument("--objects", type=Path, required=True)
    backup.add_argument("--target", type=Path, required=True)
    backup.add_argument("--consistency-note", required=True)
    backup.add_argument("--execute", action="store_true")
    verify = commands.add_parser("verify")
    verify.add_argument("--package", type=Path, required=True)
    restore = commands.add_parser("restore-plan")
    restore.add_argument("--package", type=Path, required=True)
    restore.add_argument("--target", type=Path, required=True)
    restore.add_argument("--database-name", required=True)
    restore.add_argument("--bucket", required=True)
    restore.add_argument("--execute", action="store_true")
    for command in (config, metrics, monitor, dependency, backup, verify, restore):
        command.add_argument("--report", type=Path)
    args = parser.parse_args(argv)
    try:
        if args.command == "config-check":
            report = config_check(args.root)
        elif args.command == "metrics":
            if args.max_queue < 0 or not 0 <= args.max_failure_rate <= 1:
                raise ValueError("Invalid alert thresholds")
            report = summarize_metrics(
                json.loads(args.snapshot.read_text(encoding="utf-8")),
                args.max_queue,
                args.max_failure_rate,
            )
        elif args.command == "observe":
            report = observe(args.base_url, args.execute)
        elif args.command == "dependency-check":
            report = asyncio.run(dependency_check(args.execute))
        elif args.command == "backup":
            report = create_backup(
                args.database_dump,
                args.objects,
                args.target,
                args.consistency_note,
                not args.execute,
            )
        elif args.command == "verify":
            report = verify_backup(args.package)
        else:
            report = restore_plan(
                args.package, args.target, args.database_name, args.bucket, args.execute
            )
        if args.report:
            write_json(args.report, report)
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return (
            1
            if (
                report.get("valid") is False
                or report.get("healthy") is False
                or report.get("alerts")
                or report.get("metrics", {}).get("alerts")
            )
            else 0
        )
    except Exception as exc:
        # Known validation errors are controlled messages; other errors remain classified.
        print(
            json.dumps({"error": str(exc) if isinstance(exc, ValueError) else type(exc).__name__})
        )
        return 2


if __name__ == "__main__":
    sys.exit(main())
