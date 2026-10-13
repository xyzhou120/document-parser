"""方案 C2：确定性候选检索（无模型、无 I/O）。"""

from app.rules.spec import RuleSpec
from app.services.candidate_retrieval import (
    FieldSpec,
    build_segments,
    find_units,
    normalize,
    page_readable,
    retrieve,
    value_in_text,
)


def native(text, x0, y0, x1, y1):
    return {"text": text, "bbox": [x0, y0, x1, y1], "source": "native"}


def ocr(text, x0, y0, x1, y1, label="text"):
    return {"text": text, "bbox": [x0, y0, x1, y1], "source": "ocr", "label": label}


def page(number, blocks=None, text="", tables=None, **extra):
    return {"page_number": number, "width": 600, "height": 800, "text": text,
            "text_blocks": blocks or [], "tables": tables or [], **extra}


def rule(field_key="residual_gas_content_measured", name="残余瓦斯含量实测值", unit="m³/t",
         data_type="float", synonyms=("残余瓦斯含量", "含量测定"), multi=True, retrieval=None):
    agent = {"synonyms": list(synonyms)}
    if retrieval:
        agent["retrieval"] = retrieval
    return RuleSpec(field_key=field_key, field_name_cn=name, unit=unit, data_type=data_type,
                    multi_instance=multi, strategy={"agent_extract": agent})


def spec(**kwargs):
    return FieldSpec.from_rule(rule(**kwargs))


# --- 规范化 -----------------------------------------------------------------------

def test_normalize_handles_fullwidth_ocr_markup_and_spacing():
    assert normalize("残余瓦斯含量（ｍ３）：２．２３") == "残余瓦斯含量(m³):2.23"
    assert normalize("可解吸瓦斯量 $ m^{3}/t $：1.02") == "可解吸瓦斯量m³/t:1.02"
    assert normalize("瓦斯含量(m3/t)") == "瓦斯含量(m³/t)"


def test_units_respect_word_boundaries():
    assert find_units(normalize("2.46 m³/t")) == ["m³/t"]
    # 英文单词中的单独字母 t 不是吨单位。
    assert "t" not in find_units(normalize("Table 2 report"))
    assert find_units(normalize("压力 0.20 MPa")) == ["MPa"]


def test_value_check_is_numeric_and_literal():
    assert value_in_text("2.46", "瓦斯含量(m3/t): 2.46", numeric=True)
    assert value_in_text("2.460", "瓦斯含量 2.46", numeric=True)  # 数值相等。
    assert not value_in_text("2.4", "瓦斯含量 2.46", numeric=True)
    assert not value_in_text("2.46", "瓦斯含量 12.46", numeric=True)
    assert value_in_text("采区水仓", "评价单元：采区水仓控制评价范畴", numeric=False)
    assert not value_in_text("", "anything", numeric=False)


# --- 片段和窗口 -----------------------------------------------------------------

def test_native_spans_on_one_line_form_one_segment_with_union_bbox():
    p = page(3, blocks=[
        native("残余瓦斯含量", 50, 100, 120, 110),
        native("2.23", 130, 101, 160, 111),
        native("m³/t", 165, 100, 190, 110),
    ], text="残余瓦斯含量 2.23 m³/t")
    segments = build_segments(p)
    assert len(segments) == 1
    assert segments[0].text == "残余瓦斯含量 2.23 m³/t"
    assert segments[0].bbox == [50, 100, 190, 111]


def test_strong_candidate_has_label_number_unit_and_location():
    p = page(10, blocks=[native("残余瓦斯含量 2.23 m³/t", 50, 100, 300, 110)])
    result = retrieve(spec(), [p], max_candidates=8)
    assert [c.strength for c in result.candidates] == ["strong"]
    cand = result.candidates[0]
    assert cand.numbers == ["2.23"] and cand.units == ["m³/t"]
    assert cand.page_number == 10 and cand.source == "native"
    assert cand.bbox == [50, 100, 300, 110]
    assert result.unreadable_pages == []


def test_label_only_and_number_free_lines_are_not_numeric_candidates():
    p = page(1, blocks=[native("残余瓦斯含量测定方法见附录", 50, 100, 300, 110)])
    assert retrieve(spec(), [p], max_candidates=8).candidates == []


def test_value_on_the_next_line_is_joined_into_the_window():
    p = page(2, blocks=[
        native("残余瓦斯含量", 50, 100, 120, 110),
        native("2.23 m³/t", 50, 130, 120, 140),
    ])
    result = retrieve(spec(), [p], max_candidates=8)
    cand = result.candidates[0]
    assert cand.numbers == ["2.23"] and cand.strength == "strong"
    assert "label_or_value_wrapped" in cand.hints
    assert cand.bbox == [50, 100, 120, 140]


def test_label_split_across_lines_is_found():
    p = page(2, blocks=[
        native("残余瓦斯", 50, 100, 90, 110),
        native("含量 3.38 m³/t", 50, 112, 150, 122),
    ])
    result = retrieve(spec(), [p], max_candidates=8)
    assert result.candidates and result.candidates[0].numbers == ["3.38"]


def test_thresholds_are_marked_and_rank_last():
    p = page(4, blocks=[
        native("残余瓦斯含量限值不得超过 8 m³/t", 50, 100, 300, 110),
        native("残余瓦斯含量 2.23 m³/t", 50, 200, 300, 210),
    ])
    result = retrieve(spec(), [p], max_candidates=8)
    assert [c.strength for c in result.candidates] == ["strong", "threshold"]
    assert "threshold_context" in result.candidates[1].hints


def test_mixed_window_with_a_threshold_and_a_value_is_not_threshold_only():
    p = page(4, blocks=[native("残余瓦斯含量 2.23 m³/t（限值 8 m³/t）", 50, 100, 300, 110)])
    cand = retrieve(spec(), [p], max_candidates=8).candidates[0]
    assert cand.strength == "strong"


def test_unit_mismatch_ranks_as_partial_with_hint():
    p = page(5, blocks=[native("残余瓦斯含量 2.23 MPa", 50, 100, 300, 110)])
    cand = retrieve(spec(), [p], max_candidates=8).candidates[0]
    assert cand.strength == "partial"
    assert "unit_mismatch" in cand.hints


def test_ocr_region_gives_candidate_with_region_bbox_and_ocr_hint():
    # OCR 层中的 DGC 样式表格行：单位含标记，值在冒号后。
    p = page(21, blocks=[ocr("可解吸瓦斯量 $ m^{3}/t $：1.02", 293, 1586, 604, 1620)])
    desorb = rule(field_key="desorbable_gas_content_measured", name="可解吸瓦斯含量实测值",
                  synonyms=("可解吸瓦斯量", "可解吸"))
    result = retrieve(FieldSpec.from_rule(desorb), [p], max_candidates=8)
    cand = result.candidates[0]
    assert cand.source == "ocr"
    assert cand.numbers == ["1.02"] and cand.units == ["m³/t"]
    assert cand.bbox == [293, 1586, 604, 1620]
    assert "ocr" in cand.hints and cand.strength == "strong"


def test_ocr_table_region_is_flagged_as_table():
    p = page(22, blocks=[ocr("残余瓦斯含量(m3/t) 2.46\n压力(MPa) 0.19", 50, 300, 400, 380, label="table")])
    cand = retrieve(spec(), [p], max_candidates=8).candidates[0]
    assert "table" in cand.hints and cand.source == "ocr"


def test_native_text_inside_a_table_region_is_flagged_as_table():
    tables = [{"bbox": [40, 90, 320, 130]}]
    p = page(6, blocks=[native("残余瓦斯含量 2.23 m³/t", 50, 100, 300, 110)], tables=tables)
    cand = retrieve(spec(), [p], max_candidates=8).candidates[0]
    assert cand.table and "table" in cand.hints


def test_plain_text_pages_without_geometry_still_produce_candidates():
    p = {"page_number": 7, "text": "残余瓦斯含量 2.23 m³/t\n评价单元", "image_count": 0}
    cand = retrieve(spec(), [p], max_candidates=8).candidates[0]
    assert cand.numbers == ["2.23"] and cand.bbox is None


def test_native_block_without_bbox_is_searchable_without_inventing_geometry():
    p = page(17, blocks=[{
        "block_id": "adapter-block-17", "source": "native",
        "text": "残余瓦斯含量 2.23 m³/t", "bbox": None,
    }])
    result = retrieve(spec(), [p], max_candidates=8)
    assert len(result.candidates) == 1
    candidate = result.candidates[0]
    assert candidate.bbox is None
    assert candidate.segment_ids == ["block:adapter-block-17"]


def test_automatic_ocr_source_is_canonicalized_to_ocr_for_image_policy():
    p = page(18, blocks=[{
        "block_id": "ocr-block-18", "source": "automatic_ocr",
        "text": "残余瓦斯含量 2.23 m³/t", "bbox": [1, 2, 100, 20],
    }], image_count=1, ocr_status="ok")
    result = retrieve(spec(), [p], max_candidates=8)
    assert len(result.candidates) == 1
    candidate = result.candidates[0]
    assert candidate.source == "ocr"
    assert "ocr" in candidate.hints


def test_non_numeric_fields_use_label_windows():
    unit_rule = rule(field_key="evaluation_unit_range", name="评价单元及范围", unit=None,
                     data_type="string", synonyms=("评价单元",))
    p = page(8, blocks=[native("评价单元：采区水仓控制评价范畴", 50, 100, 350, 110)])
    cand = retrieve(FieldSpec.from_rule(unit_rule), [p], max_candidates=8).candidates[0]
    assert cand.strength == "label" and cand.numbers == []


# --- 页面、排序和上限 --------------------------------------------------------------

def test_unreadable_page_is_reported_not_silently_skipped():
    failed = page(9, text="", image_count=1, ocr_status="failed")
    legacy_scan = {"page_number": 10, "text": "", "image_count": 1}
    readable = page(11, blocks=[native("残余瓦斯含量 2.23 m³/t", 50, 100, 300, 110)],
                    ocr_status="not_needed")
    result = retrieve(spec(), [failed, legacy_scan, readable], max_candidates=8)
    assert result.unreadable_pages == [9, 10]
    assert page_readable({"page_number": 1, "text": "", "image_count": 0})


def test_cap_keeps_strongest_first_and_reports_truncation():
    blocks = [native(f"残余瓦斯含量 {i}.10 m³/t", 50, 100 + i * 12, 300, 110 + i * 12) for i in range(6)]
    blocks.append(native("残余瓦斯含量 9.99 MPa", 50, 300, 300, 310))  # partial 必须排在最后。
    p = page(12, blocks=blocks)
    result = retrieve(spec(), [p], max_candidates=4)
    assert result.truncated is True
    assert result.total_matches == 7
    assert len(result.candidates) == 4
    assert all(c.strength == "strong" for c in result.candidates)


def test_duplicate_windows_on_the_same_page_are_merged():
    p = page(13, blocks=[native("残余瓦斯含量 2.23 m³/t", 50, 100, 300, 110),
                         native("残余瓦斯含量 2.23 m³/t", 50, 100, 300, 110)])
    assert len(retrieve(spec(), [p], max_candidates=8).candidates) == 1


def test_window_is_limited_to_configured_characters():
    long_line = "残余瓦斯含量 2.23 m³/t " + "说明文字" * 200
    p = page(14, blocks=[native(long_line, 50, 100, 500, 110)])
    cand = retrieve(spec(), [p], max_candidates=8, window_chars=300).candidates[0]
    assert len(cand.text) <= 300 and "2.23" in cand.text


def test_value_two_lines_below_its_label_is_followed_in_the_same_window():
    # 列表式布局：标签在一行，值在隔一行之后。
    p = page(15, blocks=[
        native("1、设计钻孔：在七联巷布置顺层钻孔", 50, 100, 320, 110),
        native("（1）钻孔孔径：94mm", 50, 112, 320, 122),
        native("（2）数量：75 个", 50, 124, 320, 134),
    ])
    desired = spec(field_key="designed", name="报告设计钻孔数", unit="个", data_type="integer",
                   synonyms=("设计钻孔",))
    cand = retrieve(desired, [p], max_candidates=8).candidates[0]
    assert "75" in cand.numbers and "label_or_value_wrapped" in cand.hints
    assert cand.bbox == [50, 100, 320, 134]
