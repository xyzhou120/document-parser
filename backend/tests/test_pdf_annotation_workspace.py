import hashlib
import json

import pymupdf
import pytest

from scripts.pdf_annotation_workspace import (
    build_workspace,
    canvas_bbox_to_pdf,
    parse_pages,
)


def sample_package(tmp_path):
    pdf = tmp_path / "sample.pdf"
    document = pymupdf.open()
    page = document.new_page(width=200, height=100)
    page.insert_text((20, 30), "actual 12")
    document.save(pdf)
    digest = hashlib.sha256(pdf.read_bytes()).hexdigest()
    manifest = {"checklist_type": "gas_report_agent", "documents": [{"id": 7,
        "sha256": digest, "copied_file": "samples/sample.pdf", "pages": 1, "split": "dev"}]}
    (tmp_path / "samples").mkdir()
    (tmp_path / "samples/sample.pdf").write_bytes(pdf.read_bytes())
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    return manifest_path


def test_workspace_renders_requested_pages_and_writes_draft_without_predictions(tmp_path):
    manifest = sample_package(tmp_path)
    result = build_workspace(manifest, 7, "1", tmp_path / "out")
    assert result["status"] == "DRAFT"
    assert result["prediction_imported"] is False
    draft = json.loads((tmp_path / "out/starter-draft.json").read_text(encoding="utf-8"))
    assert len({f["field_key"] for f in draft["documents"][0]["fields"]}) == 7
    assert all(f["status"] == "DRAFT" for f in draft["documents"][0]["fields"])
    assert (tmp_path / "out/pages/page-0001.png").is_file()
    assert "PDF_POINT" in (tmp_path / "out/index.html").read_text(encoding="utf-8")


def test_workspace_rejects_source_tamper_and_existing_output(tmp_path):
    manifest = sample_package(tmp_path)
    (tmp_path / "samples/sample.pdf").write_bytes(b"tampered")
    with pytest.raises(ValueError, match="SHA256"):
        build_workspace(manifest, 7, "1", tmp_path / "out")


def test_geometry_and_page_bounds_are_strict():
    assert canvas_bbox_to_pdf([10, 5, 20, 15], 100, 50, 200, 100) == [20, 10, 40, 30]
    with pytest.raises(ValueError):
        canvas_bbox_to_pdf([0, 0, 101, 2], 100, 50, 200, 100)
    assert parse_pages("1,3-4", 4) == [1, 3, 4]
    with pytest.raises(ValueError):
        parse_pages("0", 4)

