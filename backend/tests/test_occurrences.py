"""Preserve document occurrences and evidence without selecting a business truth."""

import asyncio
from unittest.mock import AsyncMock

from app.parsers.ocr_client import PaddleOCRClient
from app.rules.engine import RuleEngine
from app.rules.spec import RuleSpec
from app.rules.strategies.base import Instance, build_prompt
from app.rules.strategies.llm import LLMStrategy
from app.rules.strategies.table import TableStrategy


def rule(**kwargs):
    return RuleSpec(field_key="gas", field_name_cn="瓦斯含量", data_type="float", **kwargs)


def test_same_page_same_value_at_two_locations_is_preserved():
    spec = rule(
        strategy={
            "methods": ["keyword_search", "regex"],
            "keyword_search": {"regex": r"瓦斯含量：(\d+)"},
        }
    )
    text = "A 测点瓦斯含量：8；B 测点瓦斯含量：8"
    field, detail = asyncio.run(RuleEngine().extract_one([{"page_number": 1, "text": text}], spec))
    assert field.field_value is None
    assert detail["validation_status"] == "multiple_occurrences"
    assert detail["instance_count"] == 2
    assert len({item["occurrence_id"] for item in detail["instances"]}) == 2
    for item in detail["instances"]:
        assert text[item["start_offset"] : item["end_offset"]] == "8"
        assert item["methods"] == ["keyword_search", "regex"]


def test_legacy_recent_rule_does_not_choose_later_page():
    spec = rule(
        multi_instance=True,
        merge_strategy="most_recent",
        strategy={"keyword_search": {"regex": r"瓦斯含量：(\d+)"}},
    )
    field, detail = asyncio.run(
        RuleEngine().extract_one(
            [
                {"page_number": 1, "text": "瓦斯含量：8"},
                {"page_number": 5, "text": "瓦斯含量：9"},
            ],
            spec,
        )
    )
    assert field.field_value is None
    assert detail["validation_status"] == "multiple_values"
    assert [item["value"] for item in detail["instances"]] == ["8", "9"]


def test_legacy_top_level_regex_string_remains_executable():
    spec = rule(strategy={"regex": r"瓦斯含量：(\d+)"})
    field, detail = asyncio.run(
        RuleEngine().extract_one([{"page_number": 1, "text": "瓦斯含量：8"}], spec)
    )
    assert field is not None and field.field_value == "8"
    assert detail["instances"][0]["source"] == "regex"


def test_table_all_rows_keeps_equal_values_and_row_identity():
    spec = rule(
        dimensions=["workface"],
        strategy={
            "table_lookup": {"header_keywords": ["瓦斯含量"], "row_position": "all_rows"},
            "dimension_bindings": {"workface": {"header_keywords": ["工作面"]}},
        },
    )
    values = asyncio.run(
        TableStrategy().run(
            spec,
            [
                {
                    "page_number": 1,
                    "tables": [
                        {
                            "table_id": "t1",
                            "title": "实测",
                            "rows": [["工作面", "瓦斯含量"], ["101", "8"], ["102", "8"]],
                            "cells": [
                                {"row_index": 1, "column_index": 1, "bbox": [10, 20, 30, 40]}
                            ],
                        }
                    ],
                }
            ],
        )
    )
    assert len(values) == 2
    assert [item.row_index for item in values] == [1, 2]
    assert values[0].dimensions["workface"]["value"] == "101"
    assert values[1].dimensions["workface"]["value"] == "102"
    assert values[0].bbox == {"x0": 10, "y0": 20, "x1": 30, "y1": 40}
    assert values[1].bbox is None
    assert values[1].table_row == ["102", "8"]


def test_dimensions_do_not_leak_across_workface_paragraphs():
    spec = rule(
        dimension_schema={"workface": {"required": True}},
        strategy={
            "keyword_search": {"regex": r"瓦斯含量：(\d+)"},
            "dimension_bindings": {"workface": {"regex": r"(\d+)工作面"}},
        },
    )
    field, detail = asyncio.run(
        RuleEngine().extract_one(
            [
                {
                    "page_number": 1,
                    "text": "101工作面瓦斯含量：8。\n瓦斯含量：8。\n102工作面瓦斯含量：9",
                }
            ],
            spec,
        )
    )
    first, unbound, last = detail["instances"]
    assert first["dimensions"]["workface"]["value"] == "101"
    assert last["dimensions"]["workface"]["value"] == "102"
    assert unbound["dimensions"] == {} and unbound["binding_status"] == "unknown"
    assert unbound["quality_issues"] == [{"code": "UNBOUND_DIMENSION", "dimension": "workface"}]
    assert field.field_value is None


def test_bbox_is_only_from_parser_and_offsets_do_not_fake_a_rectangle():
    spec = rule(strategy={"keyword_search": {"regex": r"瓦斯含量：(\d+)"}})
    field, detail = asyncio.run(
        RuleEngine().extract_one([{"page_number": 1, "text": "瓦斯含量：8"}], spec)
    )
    assert field.evidence.locator is None
    assert detail["instances"][0]["bbox"] is None
    assert field.evidence.location_status == "text_offset"
    page = {
        "page_number": 1,
        "text": "瓦斯含量：8",
        "text_blocks": [{"start_offset": 0, "end_offset": 6, "bbox": [10, 20, 100, 40]}],
    }
    field, detail = asyncio.run(RuleEngine().extract_one([page], spec))
    assert field.evidence.locator.bbox.x0 == 10
    assert detail["instances"][0]["location_status"] == "text_block"


def test_llm_dimensions_and_coordinates_are_not_trusted_and_repeated_quotes_survive():
    spec = rule(
        dimensions=["workface"],
        strategy={"dimension_bindings": {"workface": {"regex": r"(\d+)工作面"}}},
    )
    strategy = LLMStrategy()
    strategy._chat = AsyncMock(
        return_value='{"instances":[{"value":"8","page_num":1,"context":"瓦斯含量：8",'
        '"dimensions":{"workface":"999"},"bbox":[1,1,2,2]}]}'
    )
    values = asyncio.run(
        strategy.run(
            spec, [{"page_number": 1, "text": "101工作面瓦斯含量：8。\n102工作面瓦斯含量：8"}]
        )
    )
    assert len(values) == 2
    assert [item.dimensions["workface"]["value"] for item in values] == ["101", "102"]
    assert all(item.bbox is None for item in values)


def test_no_position_evidence_never_collapses_separate_opaque_occurrences():
    items = [Instance("8", 1), Instance("8", 1)]
    occurrences, fusion = RuleEngine()._fuse_candidates(rule(), {"table_lookup": items})
    assert len(occurrences) == 2
    assert fusion["selected"] is None


def test_unknown_unit_stays_unknown_instead_of_inheriting_rule_unit():
    spec = rule(unit="m³/t", strategy={"keyword_search": {"regex": r"瓦斯含量：(\d+)"}})
    field, detail = asyncio.run(
        RuleEngine().extract_one([{"page_number": 1, "text": "瓦斯含量：8"}], spec)
    )
    assert detail["instances"][0]["unit"] == ""
    assert detail["instances"][0]["quality_issues"][0]["code"] == "UNIT_UNVERIFIED"
    assert detail["validation_status"] == "review_required"
    assert field.field_value == "8"


def test_unit_mismatch_and_unverified_value_role_are_visible_without_deleting_fact():
    spec = rule(
        unit="m³/t",
        value_kind="measured",
        strategy={"keyword_search": {"regex": r"瓦斯含量：(\d+)"}},
    )
    field, detail = asyncio.run(
        RuleEngine().extract_one([{"page_number": 1, "text": "瓦斯含量：8 m³/min"}], spec)
    )
    assert field.field_value == "8"
    item = detail["instances"][0]
    assert item["unit"] == "m³/min"
    assert {issue["code"] for issue in item["quality_issues"]} == {
        "UNIT_MISMATCH",
        "VALUE_KIND_UNVERIFIED",
    }
    assert detail["validation_status"] == "review_required"


def test_prompt_includes_field_semantics_and_requires_all_occurrences():
    prompt = build_prompt(
        rule(
            dimensions=["point"], value_kind="measured", field_description="逐测点提取实测瓦斯含量"
        ),
        "原文",
    )
    assert "逐测点提取实测瓦斯含量" in prompt
    assert "point" in prompt and "measured" in prompt
    assert "每处出现" in prompt and "不要选择最佳值" in prompt


def test_ocr_missing_box_never_becomes_full_page_evidence():
    client = PaddleOCRClient()
    for payload in ({"rec_texts": ["8"], "rec_scores": [0.9]}, {"text": "8"}, {"texts": ["8"]}):
        blocks = client._parse_blocks(payload, 1, 1800, 2600)
        assert len(blocks) == 1
        assert blocks[0].locator is None
        assert blocks[0].location_status == "unavailable"


def test_real_ocr_pixel_box_is_preserved_with_coordinate_system():
    blocks = PaddleOCRClient()._parse_blocks(
        {"rec_texts": ["瓦斯含量：8"], "rec_boxes": [[10, 20, 100, 40]], "rec_scores": [0.9]},
        1,
        1800,
        2600,
    )
    assert blocks[0].locator.coordinate_system.value == "PIXEL"
    assert blocks[0].locator.bbox.x0 == 10
    spec = rule(strategy={"keyword_search": {"regex": r"瓦斯含量：(\d+)"}})
    field, detail = asyncio.run(
        RuleEngine().extract_one(
            [
                {
                    "page_number": 1,
                    "text": "瓦斯含量：8",
                    "text_blocks": [
                        {
                            "start_offset": 0,
                            "end_offset": 6,
                            "coordinate_system": "PIXEL",
                            "bbox": [10, 20, 100, 40],
                        }
                    ],
                }
            ],
            spec,
        )
    )
    assert field.evidence.locator.coordinate_system.value == "PIXEL"
    assert detail["instances"][0]["coordinate_system"] == "PIXEL"
