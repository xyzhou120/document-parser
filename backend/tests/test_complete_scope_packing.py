"""Lossless bounded native windows; OCR/table/source boundaries stay explicit."""

from app.services.candidate_retrieval import FieldSpec
from app.services.retrieval_plan import RangeShard, ScanCursor, scan_candidates

SPEC = FieldSpec("gas", "瓦斯压力", ["瓦斯压力"], "MPa", True, True)


def scan(pages, size=300, cursor=None, shard=None):
    return scan_candidates(SPEC, pages, shard or RangeShard("complete_scope-v2", "p1-p1", 1, 1),
        window_chars=size, content_revision="fixed-revision", rule_semantic_hash="fixed-rule", cursor=cursor)


def page(lines, *, sources=None):
    return {"page_number": 1, "text_blocks": [{"block_id": f"b{i}", "text": line,
        "source": (sources or ["native"] * len(lines))[i], "bbox": None} for i, line in enumerate(lines)]}


def test_short_native_lines_pack_into_fewer_bounded_calls_without_losing_last_value():
    lines = [f"第{i}行说明" for i in range(30)] + ["末尾瓦斯压力 0.20 MPa"]
    result = scan([page(lines)])
    assert len(result.candidates) < 5
    assert all(len(candidate.text) <= 300 for candidate in result.candidates)
    assert "\n".join(c.text for c in result.candidates) == "\n".join(lines)
    assert "0.20" in result.candidates[-1].text
    spans = [span for candidate in result.candidates for span in candidate.segment_ids]
    assert spans == [f"block:b{i}@0:{len(line)}" for i, line in enumerate(lines)]
    assert all(candidate.bbox is None for candidate in result.candidates)
    # Batch size eight previously required four model calls for these 31 lines.
    assert (len(result.candidates) + 7) // 8 == 1


def test_long_blocks_preserve_every_character_and_do_not_join_short_neighbors():
    long_text = "数值ABCDE" * 80 + "末尾0.18"
    result = scan([page(["之前短行", long_text, "之后短行"])], size=80)
    long_parts = [candidate for candidate in result.candidates if candidate.segment_ids[0].startswith("block:b1@")]
    assert "".join(candidate.text for candidate in long_parts) == long_text
    assert all(len(candidate.segment_ids) == 1 for candidate in long_parts)
    assert result.candidates[0].text == "之前短行"
    assert result.candidates[-1].text == "之后短行"


def test_pages_and_raw_producer_sources_never_merge_and_legacy_page_cursor_resumes():
    first = page(["原文A", "公式B", "原文C"], sources=["native", "formula", "native"])
    second = {"page_number": 2, "text_blocks": [{"block_id": "p2", "text": "第二页末尾1.23MPa", "source": "native", "bbox": None}]}
    shard = RangeShard("complete_scope-v2", "p1-p2", 1, 2)
    result = scan([first, second], shard=shard)
    assert [c.original_source for c in result.candidates] == ["native", "formula", "native"]
    resumed = scan([first, second], shard=shard,
        cursor=ScanCursor("fixed-revision", 1, 0, "p1-p2", False))
    assert [c.page_number for c in resumed.candidates] == [2]
    assert resumed.done and "1.23" in resumed.candidates[0].text


def test_ocr_lines_and_table_rows_are_not_packed():
    p = {"page_number": 1, "text_blocks": [
        {"block_id": "ocr", "source": "automatic_ocr", "text": "OCR行一\nOCR行二", "bbox": None},
        {"block_id": "t1", "source": "ocr", "label": "table", "text": "样品A 0.1\n样品B 0.2", "bbox": [0, 0, 100, 30]},
    ]}
    result = scan([p])
    assert len(result.candidates) == 4
    assert all(candidate.source == "ocr" and len(candidate.segment_ids) == 1 for candidate in result.candidates)
    assert sum(candidate.table for candidate in result.candidates) == 2


def test_numeric_tokens_on_adjacent_lines_are_kept_separate_in_prompt_hints():
    result = scan([page(["3.42", "0.23"])])
    assert result.candidates[0].numbers == ["3.42", "0.23"]


def test_native_segments_do_not_pack_across_an_intervening_ocr_block():
    p = page(["原文段A", "扫描文字", "原文段B"], sources=["native", "automatic_ocr", "native"])
    result = scan([p])
    assert len(result.candidates) == 3
    assert not any("原文段A" in c.text and "原文段B" in c.text for c in result.candidates)
