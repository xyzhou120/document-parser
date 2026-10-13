import csv
import io
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch
from uuid import uuid4

import pytest
from fastapi import HTTPException, UploadFile

from app.api.parse import (
    compare_runs,
    export_run_facts,
    get_parse_run,
    get_run_facts,
    upload_and_parse,
)


@pytest.mark.asyncio
async def test_invalid_checklist_is_rejected_before_storage_upload():
    with (
        patch("app.api.parse.rule_repository.list_checklist_types", AsyncMock(return_value=[])),
        patch("app.api.parse.minio_client.upload") as upload,
    ):
        with pytest.raises(HTTPException) as error:
            await upload_and_parse(
                UploadFile(filename="sample.pdf", file=io.BytesIO(b"%PDF-1.4")),
                "mine",
                "煤矿",
                "invalid",
                "2026-10",
                db=Mock(),
            )
    assert error.value.status_code == 422
    upload.assert_not_called()


@pytest.mark.asyncio
async def test_database_save_failure_removes_only_the_newly_uploaded_object():
    db = Mock(
        add=Mock(),
        flush=AsyncMock(side_effect=RuntimeError("db unavailable")),
        rollback=AsyncMock(),
    )
    with (
        patch(
            "app.api.parse.rule_repository.list_checklist_types",
            AsyncMock(return_value=[{"checklist_type": "gas"}]),
        ),
        patch(
            "app.api.parse.rule_repository.get_rules_for_checklist",
            AsyncMock(return_value=[object()]),
        ),
        patch("app.api.parse.minio_client.upload", return_value="uploaded"),
        patch("app.api.parse.minio_client.delete") as delete,
    ):
        with pytest.raises(HTTPException) as error:
            await upload_and_parse(
                UploadFile(filename="sample.pdf", file=io.BytesIO(b"%PDF-1.4")),
                "mine",
                "煤矿",
                "gas",
                "2026-10",
                db=db,
            )
    assert error.value.status_code == 500
    delete.assert_called_once()
    assert delete.call_args.args[0].startswith("uploads/")
    db.rollback.assert_awaited_once()


@pytest.mark.asyncio
async def test_parse_details_preserve_missing_fields_and_quality():
    run_id, document_id = uuid4(), uuid4()
    run = SimpleNamespace(
        parse_run_id=run_id,
        document_id=document_id,
        status="COMPLETED",
        started_at=None,
        completed_at=None,
        duration_seconds=2,
        fields_extracted=0,
        mean_confidence=None,
        error_message=None,
        quality_status="FAILED",
        extraction_profile={
            "missing_fields": [{"field_key": "gas", "required": True}],
            "warnings": ["存在必填字段未提取到"],
        },
    )
    db = Mock(
        get=AsyncMock(side_effect=[run, None]),
        execute=AsyncMock(
            return_value=Mock(scalars=Mock(return_value=Mock(all=Mock(return_value=[]))))
        ),
    )
    response = await get_parse_run(run_id, db=db)
    assert response.quality_status == "FAILED"
    assert response.missing_fields[0]["field_key"] == "gas"
    assert response.warnings == ["存在必填字段未提取到"]


def fact_db(fields=None, revisions=None, document_id=None):
    run = SimpleNamespace(
        parse_run_id=uuid4(), document_id=document_id or uuid4(), extraction_profile={}
    )
    results = [Mock(scalars=Mock(return_value=Mock(all=Mock(return_value=fields or []))))]
    results.append(Mock(scalars=Mock(return_value=Mock(all=Mock(return_value=revisions or [])))))
    return run, Mock(get=AsyncMock(return_value=run), execute=AsyncMock(side_effect=results))


@pytest.mark.asyncio
async def test_fact_view_default_keeps_automatic_unknown_confidence():
    row = SimpleNamespace(
        field_key="gas",
        field_name="Gas",
        extraction_method="agent_extract",
        confidence=0.0,
        extraction_detail={},
        field_value=None,
        instances=[{"value": "120", "page_num": 1, "context": "120"}],
    )
    run, db = fact_db([row])
    result = await get_run_facts(run.parse_run_id, db=db)
    assert result.view == "automatic"
    assert result.facts[0].confidence.score is None
    assert result.facts[0].value == "120"
    assert db.execute.await_count == 2


@pytest.mark.asyncio
async def test_explicit_reviewed_view_does_not_fall_back_to_automatic():
    run, db = fact_db(revisions=[])
    result = await get_run_facts(run.parse_run_id, view="reviewed", db=db)
    assert result.view == "reviewed"
    assert result.facts == []
    assert db.execute.await_count == 2


@pytest.mark.asyncio
async def test_fact_export_contains_all_facts_and_prevents_csv_formula_execution():
    row = SimpleNamespace(
        field_key="name",
        field_name="name",
        extraction_method="keyword",
        confidence=None,
        extraction_detail={},
        field_value=None,
        instances=[
            {"value": f"=formula{i}", "page_num": 1, "context": "source"} for i in range(150)
        ],
    )
    run, db = fact_db([row])
    result = await export_run_facts(run.parse_run_id, format="csv", db=db)
    content = list(csv.DictReader(io.StringIO(result.body.decode("utf-8-sig"))))
    assert len(content) == 150
    assert all(r["value"].startswith("'=formula") for r in content)


@pytest.mark.asyncio
async def test_run_comparison_rejects_cross_document_runs():
    run1, _ = fact_db()
    run2, _ = fact_db()
    empty = Mock(scalars=Mock(return_value=Mock(all=Mock(return_value=[]))))
    db = Mock(get=AsyncMock(side_effect=[run1, run2]), execute=AsyncMock(return_value=empty))
    with pytest.raises(HTTPException) as error:
        await compare_runs(run2.parse_run_id, other_run_id=run1.parse_run_id, db=db)
    assert error.value.status_code == 422
