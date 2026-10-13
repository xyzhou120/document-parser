import json

import pytest

from scripts.platform_operations import (
    create_backup,
    restore_plan,
    summarize_metrics,
    verify_backup,
)


def dump_and_objects(tmp_path):
    dump = tmp_path / "db.dump"
    dump.write_bytes(b"PGDMP" + b"safe dump")
    objects = tmp_path / "objects"
    objects.mkdir()
    (objects / "a.pdf").write_bytes(b"pdf")
    return dump, objects


def test_backup_manifest_is_sha256_verified_and_dry_run_is_non_mutating(tmp_path):
    dump, objects = dump_and_objects(tmp_path)
    target = tmp_path / "package"
    result = create_backup(dump, objects, target, "write freeze recorded", dry_run=True)
    assert result["dry_run"] is True
    assert not target.exists()
    create_backup(dump, objects, target, "write freeze recorded", dry_run=False)
    assert verify_backup(target)["valid"]
    (target / "objects/a.pdf").write_bytes(b"tampered")
    with pytest.raises(ValueError, match="SHA256"):
        verify_backup(target)


def test_restore_requires_new_isolated_target_and_never_overwrites(tmp_path):
    dump, objects = dump_and_objects(tmp_path)
    package = tmp_path / "package"
    create_backup(dump, objects, package, "freeze", dry_run=False)
    plan = restore_plan(package, tmp_path / "staged", "restore_db", "restore-bucket")
    assert plan["dry_run"] is True
    with pytest.raises(ValueError, match="must not exist"):
        restore_plan(package, package, "restore_db", "restore-bucket")
    with pytest.raises(ValueError, match="isolated"):
        restore_plan(package, tmp_path / "x", "bad-name;drop", "restore-bucket")
    with pytest.raises(ValueError, match="restore"):
        restore_plan(package, tmp_path / "x", "document_parser", "documents")
    restore_plan(package, tmp_path / "stage", "restore_db", "restore-bucket", execute=True)
    assert verify_backup(tmp_path / "stage/package")["valid"]


@pytest.mark.parametrize("attack", ["../outside", "C:/outside", "objects/../../outside"])
def test_manifest_rejects_path_escape(tmp_path, attack):
    dump, objects = dump_and_objects(tmp_path)
    package = tmp_path / "package"
    create_backup(dump, objects, package, "freeze", dry_run=False)
    manifest = json.loads((package / "manifest.json").read_text(encoding="utf-8"))
    manifest["files"][1]["path"] = attack
    (package / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(ValueError, match="Unsafe"):
        verify_backup(package)


def test_dump_format_and_target_overlap_are_rejected(tmp_path):
    dump, objects = dump_and_objects(tmp_path)
    with pytest.raises(ValueError, match="overlap"):
        create_backup(dump, objects, objects / "package", "freeze")
    dump.write_bytes(b"plain SQL")
    with pytest.raises(ValueError, match="custom"):
        create_backup(dump, objects, tmp_path / "package", "freeze")


def test_empty_metrics_preserve_unknown_values():
    result = summarize_metrics({"runs": [{"duration_seconds": float("nan")}], "queue_depth": True})
    assert result["failure_rate"] is None
    assert result["queue_depth"] is None
    assert result["durations"]["p95_seconds"] is None


def test_migration_chain_matches_all_current_model_columns():
    import importlib.util
    from pathlib import Path
    from types import SimpleNamespace

    from alembic.config import Config
    from alembic.script import ScriptDirectory
    from sqlalchemy.dialects.postgresql import dialect

    from migrations.metadata import load_metadata

    config = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
    directory = ScriptDirectory.from_config(config)
    captured = {}
    indexes = {}

    def add_column(table_name, column, **kwargs):
        # Additive v2 migrations extend ParseRun without replacing the baseline.
        captured[table_name] = (*captured[table_name], column)

    for migration in reversed(list(directory.walk_revisions())):
        spec = importlib.util.spec_from_file_location(f"revision_{migration.revision}", migration.path)
        revision = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(revision)
        revision.op = SimpleNamespace(
            create_table=lambda name, *items, **kwargs: captured.update({name: items}),
            create_index=lambda name, table, columns, **kwargs: indexes.update({name: (table, tuple(columns))}),
            execute=lambda *args, **kwargs: None,
            add_column=add_column,
            create_foreign_key=lambda *args, **kwargs: None,
            create_check_constraint=lambda *args, **kwargs: None,
            create_unique_constraint=lambda *args, **kwargs: None,
            f=lambda value: value,
        )
        revision.upgrade()
    metadata = load_metadata()
    assert captured.keys() == metadata.tables.keys()
    for name, table in metadata.tables.items():
        actual = {item.name: item for item in captured[name] if hasattr(item, "type")}
        assert set(actual) == set(table.columns.keys())
        for column in table.columns:
            assert actual[column.name].nullable == column.nullable
            assert str(actual[column.name].type.compile(dialect=dialect())) == str(column.type.compile(dialect=dialect()))
        for index in table.indexes:
            assert indexes[index.name] == (name, tuple(column.name for column in index.columns))


def test_generated_bootstrap_files_match_metadata_and_include_audit_guards():
    from pathlib import Path

    from migrations.bootstrap import AUDIT_TABLES, canonical_sql
    from migrations.metadata import load_metadata

    root = Path(__file__).resolve().parents[2]
    expected = canonical_sql()
    assert (root / "backend/migrations/init.sql").read_text(encoding="utf-8") == expected
    assert (root / "deploy/dependencies/postgres/init/001_schema.sql").read_text(encoding="utf-8") == expected
    for table in load_metadata().tables:
        assert f"CREATE TABLE IF NOT EXISTS {table}" in expected
    for table in AUDIT_TABLES:
        assert f"CREATE TRIGGER {table}_immutable_audit" in expected


def test_metrics_are_observations_and_alert_only_on_thresholds():
    snapshot = {
        "queue_depth": 4,
        "healthy": True,
        "runs": [
            {"status": "SUCCESS", "duration_seconds": 2},
            {"status": "FAILED", "duration_seconds": 4},
        ],
        "stage_logs": [{"stage": "extract", "duration_seconds": 1.5}],
    }
    report = summarize_metrics(snapshot, max_queue=10, max_failure_rate=0.6)
    assert report["terminal_runs"] == 2
    assert report["durations"]["p95_seconds"] == 4
    assert report["stages"]["extract"]["count"] == 1
    assert report["alerts"] == []
    assert "benchmark" in report["scope"]
