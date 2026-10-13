"""P19 cross-module source provenance gates; no model service or database."""

from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.services.candidate_retrieval import FieldSpec, retrieve
from app.services.content_adapter import ContentAdapter
from app.services.content_preparation_service import ContentPreparationService
from app.services.model_gateway import CandidateInput
from app.services.retrieval_plan import RangeShard, scan_candidates


@pytest.mark.parametrize("source", ["automatic_ocr", "ocr_layout", "ocr"])
def test_adapted_ocr_remains_ocr_and_gateway_demands_original_image(source):
    artifact = SimpleNamespace(artifact_id=uuid4(), document_id=uuid4(), file_hash="f" * 64,
        status="READY", quality={}, pages=[{"page_number": 1, "text": "瓦斯压力 1.23 MPa 实测",
            "text_blocks": [{"source": source, "block_id": "ocr-block", "text": "瓦斯压力 1.23 MPa 实测",
                "bbox": None}]}])
    adapted = ContentAdapter().adapt(artifact)
    spec = FieldSpec("pressure", "瓦斯压力", ["瓦斯压力"], "MPa", True, True)
    result = retrieve(spec, adapted.pages, max_candidates=None)
    assert len(result.candidates) == 1
    candidate = result.candidates[0]
    assert candidate.source == "ocr"
    assert candidate.original_source == source
    assert adapted.pages[0]["text_blocks"][0]["original_source"] == source
    with pytest.raises(ValueError, match="requires original image"):
        CandidateInput("c", candidate.as_prompt("c"), source=candidate.source)


def test_complete_scope_keeps_no_bbox_native_block_and_preserves_manual_source():
    artifact = SimpleNamespace(artifact_id=uuid4(), document_id=uuid4(), file_hash="f" * 64,
        status="READY", quality={}, pages=[{"page_number": 1, "text_blocks": [
            {"source": "native", "block_id": "native-block", "text": "瓦斯压力 1.23 MPa 实测", "bbox": None},
            {"source": "human_correction", "block_id": "manual-block", "text": "瓦斯压力 2.34 MPa 实测", "bbox": None},
        ]}])
    adapted = ContentAdapter().adapt(artifact)
    batch = scan_candidates(FieldSpec("pressure", "瓦斯压力", ["瓦斯压力"], "MPa", True, True),
        adapted.pages, RangeShard("complete_scope-v2", "p1-p1", 1, 1),
        content_revision="rev", rule_semantic_hash="rule")
    assert {c.source for c in batch.candidates} == {"native", "human_correction"}
    assert all(c.bbox is None for c in batch.candidates)
    assert any("native-block" in segment for c in batch.candidates for segment in c.segment_ids)
    assert any("manual-block" in segment for c in batch.candidates for segment in c.segment_ids)


def test_content_preparation_to_adapter_to_retrieval_never_promotes_ocr_to_native():
    pages = [{"page_number": 1, "text": "", "native_text": "", "text_blocks": [], "tables": []}]
    checkpoint = {"pages": {"1": {"regions": ["whole"], "scope_attested": True}},
        "regions": {"p1:whole": {"region_id": "whole", "state": "processed", "image_hash": "image-hash",
            "blocks": [{"raw_text": "瓦斯压力 1.23 MPa 实测", "bbox": None,
                        "locator": {"coordinate_system": "PDF_POINT", "page": 1}}]}}}
    prepared = ContentPreparationService._pages_with_checkpoint(pages, checkpoint)
    assert prepared[0]["text_blocks"][0]["source"] == "automatic_ocr"
    artifact = SimpleNamespace(artifact_id=uuid4(), document_id=uuid4(), file_hash="f" * 64,
        status="READY", quality={}, pages=prepared)
    adapted = ContentAdapter().adapt(artifact)
    spec = FieldSpec("pressure", "瓦斯压力", ["瓦斯压力"], "MPa", True, True)
    retrieved = retrieve(spec, adapted.pages, max_candidates=None)
    batch = scan_candidates(spec, adapted.pages, RangeShard("complete_scope-v2", "p1-p1", 1, 1),
        content_revision="rev", rule_semantic_hash="rule")
    assert retrieved.candidates and batch.candidates
    assert all(c.source == "ocr" and c.original_source == "automatic_ocr"
               for c in [*retrieved.candidates, *batch.candidates])
