import hashlib
import json
from types import SimpleNamespace

import pymupdf
import pytest

from scripts.agent_evaluation import import_results
from scripts.agent_sample_runner import (
    LocalPDFImages,
    parse_args,
    run_sample,
    seed_specs,
    sources_from_manifest,
)


@pytest.fixture
def pdf(tmp_path):
    path = tmp_path / "sample.pdf"
    doc = pymupdf.open()
    page = doc.new_page(width=300, height=300)
    page.insert_text((30, 30), "Actual holes 326. Workface 40203.")
    doc.save(path)
    doc.close()
    return path


def test_source_hash_checked_before_rendering(pdf):
    with pytest.raises(ValueError, match="hash mismatch"):
        LocalPDFImages(pdf, "0" * 64)


@pytest.mark.asyncio
async def test_images_use_verified_snapshot_and_reject_invalid_regions(pdf):
    content_hash = hashlib.sha256(pdf.read_bytes()).hexdigest()
    provider = LocalPDFImages(pdf, content_hash)
    try:
        pdf.write_bytes(b"modified after snapshot")
        assert (await provider.get(1, None)).startswith(b"\x89PNG")
        assert (await provider.get(1, [10, 10, 90, 90])).startswith(b"\x89PNG")
        assert all(r["file_hash"] == content_hash for r in provider.receipts)
        with pytest.raises(ValueError, match="region"):
            await provider.get(1, [-10, 0, 10, 10])
        with pytest.raises(ValueError, match="page"):
            await provider.get(2, None)
    finally:
        provider.close()


def test_seed_rules_share_production_definitions_and_allow_explicit_disabled_field():
    rules, digest = seed_specs(["actual_hole_count", "construction_period"])
    assert {r.field_key for r in rules} == {"actual_hole_count", "construction_period"}
    assert all(r.methods == ["agent_extract"] for r in rules)
    assert len(digest) == 64
    with pytest.raises(ValueError, match="Unknown field"):
        seed_specs(["not_a_field"])


@pytest.mark.asyncio
async def test_dry_run_validates_source_and_emits_no_predictions(pdf, monkeypatch):
    def forbidden_model(*args, **kwargs):
        raise AssertionError("dry run must never construct an agent/model client")
    monkeypatch.setattr("scripts.agent_sample_runner.ChecklistAgent", forbidden_model)
    rules, knowledge_hash = seed_specs(["actual_hole_count"])
    out = await run_sample({"id": 131, "path": str(pdf)}, rules, knowledge_hash,
                           {"max_rounds": 2}, dry_run=True)
    assert out["status"] == "DRY_RUN"
    assert out["fields"] == []
    assert out["details"] == []
    assert out["run_metadata"]["accuracy_status"] == "UNVERIFIED"
    assert out["run_metadata"]["cost"] is None
    assert not out["agent_runtime"]["image_artifacts"]


@pytest.mark.asyncio
async def test_runner_calls_real_agent_and_exports_importable_candidates(pdf):
    class FakeModel:
        def __init__(self):
            self.calls = 0
            self.chat = SimpleNamespace(completions=self)

        async def create(self, **kwargs):
            self.calls += 1
            if self.calls == 1:
                content = {"actions": [{"type": "view_page", "page_number": 1}]}
            else:
                content = {"fields": [{"field_key": "actual_hole_count", "value": 326,
                    "raw_value": "326", "unit": "个", "value_kind": "actual",
                    "source_role": "report_body", "quote": "326",
                    "evidence_ids": ["ev-000001"], "evidence_roles": {"ev-000001": ["value"]},
                    "dimensions": {"workface": {"value": "40203", "evidence_ids": ["ev-000001"], "quote": "40203"}}}]}
            return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(
                content=json.dumps(content)))], usage=SimpleNamespace(prompt_tokens=100,
                completion_tokens=100, total_tokens=200))

    rules, knowledge_hash = seed_specs(["actual_hole_count"])
    client = FakeModel()
    result = await run_sample({"id": 131, "path": str(pdf)}, rules, knowledge_hash,
                              {"max_rounds": 3, "max_total_tokens": 10000}, client=client,
                              model_config={"model_name": "fake", "model_version": "test"})
    assert client.calls == 2
    assert result["details"][0]["instances"][0]["value"] == 326
    assert result["agent_runtime"]["image_artifacts"][0]["file_hash"] == result["file_hash"]
    manifest = {"documents": [{"id": 131, "sha256": result["file_hash"], "pages": 1}]}
    imported = import_results(manifest, 131, result)
    assert imported["documents"][0]["fields"][0]["facts"][0]["raw_value"] == 326
    assert result["run_metadata"]["accuracy_status"] == "UNVERIFIED"


def test_manifest_requires_explicit_ids_and_hashes(tmp_path, pdf):
    manifest = tmp_path / "manifest.json"
    digest = hashlib.sha256(pdf.read_bytes()).hexdigest()
    manifest.write_text(json.dumps({"documents": [{"id": 131, "path": pdf.name, "sha256": digest}]}))
    source = sources_from_manifest(manifest, [131], tmp_path)[0]
    assert source["expected_hash"] == digest
    assert source["path"] == str(pdf)
    with pytest.raises(ValueError, match="Unknown sample"):
        sources_from_manifest(manifest, [999])
    with pytest.raises(SystemExit):
        parse_args(["--manifest", str(manifest), "--out-dir", str(tmp_path)])
    with pytest.raises(SystemExit):
        parse_args(["--pdf", "x.pdf", "--out-dir", str(tmp_path), "--max-rounds", "0"])


def test_manifest_prefers_portable_copied_file_and_verifies_hash_and_pages(tmp_path):
    sample_dir = tmp_path / "交接 包" / "samples" / "dev"
    sample_dir.mkdir(parents=True)
    pdf_path = sample_dir / "001.pdf"
    doc = pymupdf.open()
    doc.new_page(width=72, height=72)
    doc.save(pdf_path)
    doc.close()
    digest = hashlib.sha256(pdf_path.read_bytes()).hexdigest()
    manifest = tmp_path / "交接 包" / "manifest.json"
    manifest.write_text(json.dumps({"documents": [{"id": 1, "copied_file": "samples/dev/001.pdf",
        "path": "missing/original.pdf", "sha256": digest, "pages": 1}]}), encoding="utf-8")
    source = sources_from_manifest(manifest, [1], root=tmp_path / "missing")[0]
    assert source["resolution"] == "copied_file"
    assert source["path"] == str(pdf_path.resolve())
    pdf_path.write_bytes(b"tampered")
    with pytest.raises(ValueError, match="hash mismatch"):
        sources_from_manifest(manifest, [1])


def test_manifest_rejects_copied_file_escape_and_page_mismatch(tmp_path):
    manifest = tmp_path / "manifest.json"
    outside = tmp_path.parent / "outside.pdf"
    outside.write_bytes(b"not a pdf")
    manifest.write_text(json.dumps({"documents": [{"id": 1, "copied_file": "../outside.pdf",
        "sha256": "0" * 64, "pages": 1}]}), encoding="utf-8")
    with pytest.raises(ValueError, match="escapes"):
        sources_from_manifest(manifest, [1])


def test_sample_root_override_and_page_mismatch_are_checked(tmp_path, pdf):
    manifest = tmp_path / "elsewhere" / "manifest.json"
    manifest.parent.mkdir()
    row = {"id": 1, "copied_file": pdf.name, "pages": 2,
           "sha256": hashlib.sha256(pdf.read_bytes()).hexdigest()}
    manifest.write_text(json.dumps({"documents": [row]}))
    with pytest.raises(ValueError, match="page count mismatch"):
        sources_from_manifest(manifest, [1], sample_root=pdf.parent)
    row["pages"] = 1
    manifest.write_text(json.dumps({"documents": [row]}))
    assert sources_from_manifest(manifest, [1], sample_root=pdf.parent)[0]["path"] == str(pdf)
