import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch
from uuid import uuid4

from app.models.contracts import Box, EvidenceRef, ExtractedField, Locator
from app.rules.spec import RuleSpec
from app.services.parse_service import ParseService


def test_parse_retries_clear_error_and_find_actual_page():
    run = SimpleNamespace(
        status="FAILED",
        error_message="old failure",
        completed_at=None,
        duration_seconds=None,
        mean_confidence=None,
        fields_extracted=99,
    )
    document = SimpleNamespace(
        file_path="sample.pdf", file_name="sample.pdf", page_count=None, checklist_type="gas"
    )
    db = Mock()
    db.get = AsyncMock(
        side_effect=lambda model, key: document if model.__name__ == "Document" else run
    )
    db.commit = AsyncMock()
    db.rollback = AsyncMock()
    service = ParseService.__new__(ParseService)
    observed_paths = []

    def pages(path):
        observed_paths.append(Path(path))
        assert Path(path).exists()
        return [{"page_number": 1, "text": "背景说明"}, {"page_number": 2, "text": "钻孔45个"}]

    service.pdf_parser = Mock(extract_pages=pages, inspect_pages=Mock(return_value=[]))
    field = ExtractedField(
        field_key="borehole_count",
        field_value="45",
        extraction_method="keyword",
        confidence=0.95,
        evidence=EvidenceRef(
            page_number=2,
            locator=Locator(coordinate_system="PDF_POINT", bbox=Box(x0=0, y0=0, x1=100, y1=20)),
            quote="钻孔45个",
            confidence=0.95,
        ),
    )
    service._save_results = AsyncMock()
    rules = [RuleSpec(field_key="borehole_count", field_name_cn="钻孔数", required=True)]
    details = [{"field_key": "borehole_count", "required": True, "validation_status": "passed"}]
    with (
        patch("app.services.parse_service.minio_client.download", return_value=b"%PDF-1.4"),
        patch(
            "app.services.parse_service.rule_repository.get_rules_for_document",
            AsyncMock(return_value=rules),
        ),
        patch(
            "app.services.parse_service.rule_engine.extract_all",
            AsyncMock(return_value=([field], details)),
        ),
    ):
        asyncio.run(service.parse_document(db, uuid4(), uuid4()))
    assert run.status == "COMPLETED"
    assert run.error_message is None
    assert run.fields_extracted == 1
    assert field.evidence.page_number == 2
    assert document.page_count == 2
    assert run.quality_status == "REVIEW_REQUIRED"
    assert run.extraction_profile["rules"][0]["required"] is True
    assert run.extraction_profile["missing_fields"] == []
    service._save_results.assert_awaited_once()
    assert not observed_paths[0].exists()


def test_parse_failure_cleans_temporary_pdf_and_rolls_back():
    from app.services.parse_service import rule_snapshot
    run = SimpleNamespace(status="PENDING", error_message=None,
                          extraction_profile=rule_snapshot([RuleSpec(field_key="gas", field_name_cn="瓦斯")], "gas"))
    document = SimpleNamespace(file_path="sample.pdf", file_name="sample.pdf")
    db = Mock(
        get=AsyncMock(side_effect=[run, document, run]), commit=AsyncMock(), rollback=AsyncMock()
    )
    service = ParseService.__new__(ParseService)
    paths = []

    def fail(path):
        paths.append(Path(path))
        raise ValueError("invalid PDF")

    service.pdf_parser = Mock(extract_pages=fail)
    with patch("app.services.parse_service.minio_client.download", return_value=b"%PDF-1.4"):
        try:
            asyncio.run(service.parse_document(db, uuid4(), uuid4()))
        except ValueError:
            pass
    assert run.status == "FAILED"
    assert run.error_message == "invalid PDF"
    db.rollback.assert_awaited_once()
    assert not paths[0].exists()


def test_required_missing_and_conflict_cannot_be_quality_ok():
    for details in (
        [{"required": True, "validation_status": "missing"}],
        [{"required": False, "validation_status": "conflict"}],
    ):
        assert ParseService._quality_status(0.99, 19, 20, [], details) != "QUALITY_OK"
