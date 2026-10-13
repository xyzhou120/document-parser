"""Page batches preserve full source coverage while sharing field understanding."""

import json
from copy import deepcopy
from uuid import uuid4

import pytest

from app.services.model_gateway import CallRequest, CandidateInput, build_messages
from app.services.page_extraction import build_page_request, build_page_units


def test_final_page_and_long_page_tail_are_never_dropped():
    text = "报告正文\n" + "瓦斯含量1.23m³/t\n" * 1900 + "末尾测点Z99=7.89"
    pages = [{"page_number": 1, "text": text}, {"page_number": 29, "text": "最终页孔数123个"}]
    before = deepcopy(pages)
    units = build_page_units(pages, max_chars=6000)
    first = [unit for unit in units if unit["page_number"] == 1]
    assert len(first) > 1
    assert "".join(unit["text"] for unit in first) == text
    assert all(len(unit["text"]) <= 6000 for unit in units)
    assert units[-1]["page_number"] == 29
    assert units[-1]["text"] == "最终页孔数123个"
    assert pages == before
    assert units == build_page_units(pages, max_chars=6000)
    assert len({unit["unit_id"] for unit in units}) == len(units)


def test_short_blocks_share_one_unit_and_preserve_source_coordinates():
    units = build_page_units([{"page_number": 1, "text_blocks": [
        {"block_id": "heading", "text": "测点A", "source": "native", "bbox": [0, 0, 10, 10]},
        {"block_id": "value", "text": "含量1.23", "source": "native", "bbox": [1, 11, 12, 20]},
        {"block_id": "correction", "text": "人工校对2.34", "source": "human_correction"},
    ]}])
    assert len(units) == 2
    assert units[0]["text"] == "测点A\n含量1.23"
    assert units[0]["bbox"] == [0, 0, 12, 20]
    assert units[0]["segment_ids"] == ["heading@0:3", "value@0:6"]
    assert units[1]["source"] == "human_correction"
    assert units[1]["original_source"] == "human_correction"
    assert units[1]["bbox"] is None


def test_ocr_only_and_legacy_duplicate_regions_keep_source_and_identity():
    ocr = {"block_id": "region1", "text": "扫描测点1.23", "source": "automatic_ocr", "bbox": [1, 2, 3, 4]}
    pages = [{"page_number": 3, "text_blocks": [ocr], "ocr_blocks": [
        {"text": ocr["text"], "bbox": ocr["bbox"]},
        {"text": ocr["text"], "bbox": [5, 6, 7, 8]},
    ]}]
    units = build_page_units(pages)
    assert len(units) == 2  # source provenance is not mixed
    assert units[0]["source"] == "ocr"
    assert units[0]["original_source"] == "automatic_ocr"
    assert units[0]["text"] == ocr["text"]
    assert units[1]["text"] == ocr["text"]
    assert units[1]["bbox"] == [5, 6, 7, 8]
    assert len(build_page_units([{"page_number": 1, "ocr_blocks": [ocr]}])) == 1


def test_table_rows_cells_headers_and_tail_survive_chunking():
    table = {"table_id": "measurements", "title": "测定结果", "header": {"columns": ["测点", "值"]},
             "continuation_header": "4034工作面", "rows": [[f"A{i}", str(i)] for i in range(200)],
             "cells": [{"cell_id": "last", "row_index": 199, "column_index": 1, "text": "199"}],
             "bbox": [1, 2, 300, 500]}
    units = build_page_units([{"page_number": 7, "tables": [table]}], max_chars=250)
    assert len(units) > 1
    records = [json.loads(line) for line in "".join(unit["text"] for unit in units).splitlines()]
    assert [record["row"] for record in records if "row" in record] == table["rows"]
    assert not [record for record in records if "cell" in record]  # row and cell views must not repeat facts
    assert records[0]["header"] == table["header"]
    assert records[0]["continuation_header"] == "4034工作面"
    # These rows fit the limit: every resulting chunk contains whole JSON rows.
    assert all(json.loads(line) for unit in units for line in unit["text"].splitlines())
    assert all(unit["table"] and unit["tables"][0]["table_id"] == "measurements" for unit in units)
    assert all(unit["bbox"] == table["bbox"] for unit in units)


def test_previous_page_context_is_400_character_tail_in_reading_order():
    first = "开头" + "甲" * 500 + "末页表头测点/含量"
    units = build_page_units([{"page_number": 2, "text": "A1 1.23"},
                              {"page_number": 1, "text": first}])
    assert units[0]["page_number"] == 1
    assert units[0]["context"] == ""
    assert units[1]["context"] == first[-400:]


def test_empty_blocks_and_native_ocr_text_fallback_do_not_hide_content():
    units = build_page_units([{"page_number": 1, "text_blocks": [{"text": ""}],
                              "native_text": "电子正文", "ocr_text": "扫描补充"}])
    assert [(unit["source"], unit["text"]) for unit in units] == [
        ("native", "电子正文"), ("ocr", "扫描补充")]
    empty = build_page_units([{"page_number": 2, "text": ""}])
    assert len(empty) == 1 and empty[0]["page_number"] == 2 and empty[0]["text"] == ""


def test_multi_field_request_retains_knowledge_and_reaches_gateway_prompt():
    rules = [{"field_key": "gas", "strategy": {"agent_extract": {
        "instructions": "逐测点提取", "source_roles": ["report_sheet"]}},
        "identity_dimensions": ["measurement_point"], "cardinality": "many"},
        {"field_key": "holes", "field_name_cn": "钻孔数", "unit": "个"}]
    unit = build_page_units([{"page_number": 9, "ocr_text": "测点A含量1.23；钻孔12个"}])[0]
    request = build_page_request(unit, rules, "candidate9")
    call = CallRequest(job_id=uuid4(), fence=1, provider_key="test", dispatch_key="test",
                       checkpoint_key="test", candidates=(CandidateInput("candidate9", request, source="native"),),
                       model="test", estimated_input_tokens=100)
    envelope = json.loads(build_messages(call)[-1]["content"][0]["text"])
    assert len(envelope["candidates"]) == 1
    candidate = envelope["candidates"][0]
    assert candidate["rules"] == rules
    assert candidate["source"] == "native"
    assert candidate["original_source"] == "ocr"
    assert candidate["source_segments"][0]["source"] == "ocr"
    schema = candidate["response_schema"]["decisions"][0]
    assert schema["candidate_id"] == "candidate9"
    assert set(schema["facts"][0]) == {
        "field_key", "value", "quote", "unit", "value_kind", "dimensions", "source_role", "table_relation"}
    assert "many facts" in candidate["instruction"]
    request["rules"][0]["identity_dimensions"].append("workface")
    assert rules[0]["identity_dimensions"] == ["measurement_point"]


@pytest.mark.parametrize("size", [0, -1])
def test_invalid_unit_size_is_rejected(size):
    with pytest.raises(ValueError, match="max_chars"):
        build_page_units([], max_chars=size)
