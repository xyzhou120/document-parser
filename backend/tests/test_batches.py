"""Persistent batch recovery: idempotency, snapshot retry, safe cancellation/export."""

import hashlib
import io
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch
from uuid import uuid4

import pytest
from fastapi import HTTPException, UploadFile

from app.api.batches import (
    BatchInput,
    ItemInput,
    VersionInput,
    cancel_item,
    check_version,
    create_batch,
    export_facts,
    retry_item,
    upload_item,
)
from app.models.batches import BatchItem
from app.models.database import Document, ParseRun
from app.services.batch_service import BatchUploadSession, batch_summary, facts_csv


def make_item(**values):
    data = {
        "item_id": uuid4(),
        "batch_id": uuid4(),
        "client_key": "a.pdf",
        "file_name": "a.pdf",
        "file_hash": None,
        "file_size": None,
        "metadata_json": {
            "mine_id": "mine-a",
            "mine_name": "甲矿",
            "checklist_type": "月报",
            "reporting_period": "2026-10",
            "extraction_engine": "checklist_agent",
        },
        "document_id": None,
        "parse_run_id": None,
        "status": "PENDING",
        "version": 1,
        "attempts": 0,
        "error_message": None,
    }
    data.update(values)
    return SimpleNamespace(**data)


def fake_db(item):
    result = Mock()
    result.scalar_one_or_none.return_value = item
    return Mock(
        execute=AsyncMock(return_value=result),
        get=AsyncMock(),
        flush=AsyncMock(),
        commit=AsyncMock(),
        rollback=AsyncMock(),
        add=Mock(),
    )


def test_batch_progress_uses_latest_run_not_stale_item_state():
    items = [make_item(status="PENDING", parse_run_id="one"), make_item(status="UPLOAD_FAILED")]
    result = batch_summary(items, {"one": SimpleNamespace(status="COMPLETED")})
    assert result["completed"] == 1
    assert result["failed"] == 1
    assert result["progress_percent"] == 100
    assert result["status"] == "PARTIAL_FAILED"


def test_batch_cancelled_and_unfinished_are_distinct():
    assert batch_summary([make_item(status="CANCELLED")], {})["status"] == "CANCELLED"
    assert batch_summary([make_item(status="PENDING")], {})["terminal"] == 0


def test_csv_preserves_facts_and_prevents_formula_injection():
    text = facts_csv(
        [
            {
                "file_name": "=HYPERLINK(1)",
                "fact": {
                    "value": "+12",
                    "dimensions": {"workface": "A"},
                    "evidence": [{"page": 1}],
                },
            }
        ]
    )
    assert "'=HYPERLINK(1)" in text
    assert "'+12" in text
    assert "workface" in text
    assert "page" in text


def test_version_conflict_blocks_mutation():
    with pytest.raises(HTTPException) as error:
        check_version(make_item(version=3), 2)
    assert error.value.status_code == 409


@pytest.mark.asyncio
async def test_upload_session_links_document_and_run_before_commit():
    item = make_item()
    db = fake_db(item)
    document = Document(document_id=uuid4(), file_size=100)
    run = ParseRun(parse_run_id=uuid4(), document_id=document.document_id, status="PENDING")
    session = BatchUploadSession(db, item)
    session.add(document)
    session.add(run)

    async def verify_commit():
        assert item.document_id == document.document_id
        assert item.parse_run_id == run.parse_run_id

    db.commit.side_effect = verify_commit
    await session.commit()
    assert item.file_size == 100


@pytest.mark.asyncio
async def test_repeated_successful_upload_returns_existing_document_without_uploader():
    content = b"%PDF-1.4 already uploaded"
    item = make_item(
        document_id=uuid4(),
        parse_run_id=uuid4(),
        file_hash=hashlib.sha256(content).hexdigest(),
        version=3,
    )
    db = fake_db(item)
    db.get.return_value = SimpleNamespace(status="COMPLETED", error_message=None)
    with patch("app.api.parse.upload_and_parse", new_callable=AsyncMock) as upload:
        result = await upload_item(
            item.batch_id,
            item.item_id,
            version=1,
            file=UploadFile(io.BytesIO(content), filename="a.pdf"),
            db=db,
        )
    upload.assert_not_awaited()
    assert result["document_id"] == item.document_id


@pytest.mark.asyncio
async def test_registered_hash_cannot_be_replaced():
    item = make_item(file_hash="a" * 64)
    with pytest.raises(HTTPException) as error:
        await upload_item(
            item.batch_id,
            item.item_id,
            version=1,
            file=UploadFile(io.BytesIO(b"%PDF-1.4 other"), filename="a.pdf"),
            db=fake_db(item),
        )
    assert error.value.status_code == 409


@pytest.mark.asyncio
async def test_uploaded_task_cannot_be_cancelled():
    item = make_item(document_id=uuid4(), parse_run_id=uuid4())
    with pytest.raises(HTTPException) as error:
        await cancel_item(item.batch_id, item.item_id, VersionInput(version=1), db=fake_db(item))
    assert error.value.status_code == 409


@pytest.mark.asyncio
async def test_cancel_pending_is_persistent_and_versioned():
    item = make_item()
    db = fake_db(item)
    result = await cancel_item(item.batch_id, item.item_id, VersionInput(version=1), db=db)
    assert result["status"] == "CANCELLED"
    assert result["version"] == 2
    db.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_parse_retry_reuses_document_and_snapshot_and_incremental_parent():
    item = make_item(document_id=uuid4(), parse_run_id=uuid4(), status="PARSE_FAILED")
    original_parent = uuid4()
    snapshot = {
        "rules": [{"field_key": "count", "version": 4}],
        "extraction_engine": "checklist_agent",
    }
    previous = SimpleNamespace(
        status="FAILED",
        parse_run_id=item.parse_run_id,
        document_id=item.document_id,
        artifact_id=uuid4(),
        run_mode="INCREMENTAL",
        parent_parse_run_id=original_parent,
        requested_field_keys=["count"],
        checklist_type="月报",
        extraction_profile=snapshot,
    )
    db = fake_db(item)
    db.get.return_value = previous

    def assigned_id(obj):
        obj.parse_run_id = uuid4()

    db.add.side_effect = assigned_id
    with patch("app.api.batches.parse_document_task") as task:
        result = await retry_item(item.batch_id, item.item_id, VersionInput(version=1), db=db)
    run = db.add.call_args.args[0]
    assert run.document_id == previous.document_id
    assert run.parent_parse_run_id == original_parent
    assert run.requested_field_keys == ["count"]
    assert run.extraction_profile == snapshot
    assert run.extraction_profile is not snapshot
    assert result["parse_run_id"] != previous.parse_run_id
    task.delay.assert_called_once_with(str(item.document_id), str(run.parse_run_id))


@pytest.mark.asyncio
async def test_create_batch_rejects_duplicate_keys_before_any_commit():
    entry = ItemInput(
        client_key="x",
        file_name="x.pdf",
        mine_id="mine",
        mine_name="矿",
        checklist_type="月报",
        reporting_period="2026-10",
    )
    db = fake_db(None)
    with pytest.raises(HTTPException):
        await create_batch(BatchInput(name="batch", items=[entry, entry]), db=db)
    db.commit.assert_not_awaited()


def test_batches_use_database_uniqueness_not_filename_only():
    constraint = next(
        c for c in BatchItem.__table__.constraints if c.name == "uq_batch_item_client_key"
    )
    assert [column.name for column in constraint.columns] == ["batch_id", "client_key"]


@pytest.mark.asyncio
async def test_export_reads_all_pages_and_preserves_missing_fields():
    pairs = []
    for _ in range(103):
        document_id = uuid4()
        run = SimpleNamespace(
            parse_run_id=uuid4(),
            document_id=document_id,
            status="COMPLETED",
            extraction_profile={"missing_fields": [{"field_key": "count", "field_name": "数量"}]},
        )
        document = SimpleNamespace(
            document_id=document_id,
            file_name="report.pdf",
            mine_id="mine-a",
            mine_name="甲矿",
            checklist_type="月报",
            report_period="2026-10",
        )
        pairs.append((run, document))
    first, second, last, empty_fields = Mock(), Mock(), Mock(), Mock()
    first.all.return_value = pairs[:100]
    second.all.return_value = pairs[100:]
    last.all.return_value = []
    empty_fields.scalars.return_value.all.return_value = []
    db = Mock(execute=AsyncMock(side_effect=[first, empty_fields, second, empty_fields, last]))
    result = await export_facts(mine_id="mine-a", reporting_period="2026-10", format="json", db=db)
    assert result["run_count"] == 103
    assert result["row_count"] == 103
    assert all(row["export_note"] == "MISSING_FIELD" for row in result["items"])
    for call in (db.execute.call_args_list[0], db.execute.call_args_list[2]):
        sql = str(call.args[0])
        assert "documents.mine_id" in sql and "documents.report_period" in sql
