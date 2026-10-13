from unittest.mock import AsyncMock, Mock, patch
from uuid import uuid4

import pymupdf
import pytest

from app.config import ModelSettings, settings
from app.parsers.pdf_parser import PDFParser
from app.services.artifact_service import ArtifactService, preprocessing_profile


@pytest.mark.asyncio
async def test_ready_lookup_scopes_document_hash_and_version_and_excludes_partial():
    db = Mock(execute=AsyncMock(return_value=Mock(scalar_one_or_none=Mock(return_value=None))))
    document_id = uuid4()
    assert await ArtifactService().find_ready(db, document_id, "new-hash", "v2") is None
    statement = db.execute.call_args.args[0]
    parameters = statement.compile().params
    assert document_id in parameters.values()
    assert "new-hash" in parameters.values()
    assert "v2" in parameters.values()
    assert "READY" in parameters.values()
    assert "PARTIAL" not in parameters.values()


@pytest.mark.asyncio
async def test_failed_ocr_is_partial_and_saved_page_data_is_independent():
    pages = [{"page_number": 1, "text": "text", "text_blocks": [{"bbox": [1, 2, 3, 4]}]}]
    db = Mock(add=Mock(), flush=AsyncMock(), commit=AsyncMock())
    artifact = await ArtifactService().save(db, uuid4(), "hash", "v1", pages, {"failed_pages": [1]})
    assert artifact.status == "PARTIAL"
    pages[0]["text_blocks"][0]["bbox"][0] = 99
    assert artifact.pages[0]["text_blocks"][0]["bbox"][0] == 1
    db.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_low_quality_but_complete_ocr_retains_weak_quality_in_ready_artifact():
    db = Mock(add=Mock(), flush=AsyncMock(), commit=AsyncMock())
    artifact = await ArtifactService().save(db, uuid4(), "hash", "v1", [],
                                            {"failed_pages": [], "weak_pages": [1]})
    assert artifact.status == "READY"
    assert artifact.quality["weak_pages"] == [1]


def test_native_artifact_span_offsets_and_table_cell_geometry(tmp_path):
    path = tmp_path / "coordinates.pdf"
    with pymupdf.open() as doc:
        page = doc.new_page()
        page.insert_text((30, 30), "Measured gas: 4.5")
        doc.save(path)
    page = PDFParser().extract_pages(str(path))[0]
    block = page["text_blocks"][0]
    assert page["text"][block["start_offset"]:block["end_offset"]] == block["text"]
    assert block["bbox"][0] == 30
    assert block["coordinate_system"] == "PDF_POINT"
    assert page["native_text"] == page["text"]


def test_preprocessing_fingerprint_changes_with_ocr_deployment_but_not_api_secret():
    first = ModelSettings(base_url="http://ocr", model_name="ocr", model_version="r1", api_key="secret-one")
    with patch.dict(settings.models, {"ocr": first}):
        profile1 = preprocessing_profile()
        first.api_key = "secret-two"
        assert preprocessing_profile()["version"] == profile1["version"]
        first.model_version = "r2"
        assert preprocessing_profile()["version"] != profile1["version"]
    assert "api_key" not in str(profile1["manifest"])
    assert len(profile1["version"]) <= 50
