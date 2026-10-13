import asyncio
from unittest.mock import AsyncMock, patch

from app.parsers.ocr_client import PaddleOCRClient
from app.rules.engine import RuleEngine
from app.rules.spec import RuleSpec
from app.rules.strategies.base import Instance, parse_llm_instances
from app.rules.strategies.keyword import KeywordStrategy
from app.rules.strategies.llm import LLMStrategy
from app.rules.strategies.table import TableStrategy


def run_extract(results, *, validation=None):
    rule = RuleSpec(
        field_key="gas",
        field_name_cn="瓦斯含量",
        data_type="float",
        strategy={"methods": list(results)},
        validation=validation,
    )
    strategies = {
        method: type("Stub", (), {"name": method, "run": AsyncMock(return_value=values)})()
        for method, values in results.items()
    }
    with patch("app.rules.engine.get_strategy", side_effect=strategies.get):
        answer = asyncio.run(
            RuleEngine().extract_one([{"page_number": 1, "text": "瓦斯含量8.0，压力999"}], rule)
        )
    assert all(strategy.run.await_count == 1 for strategy in strategies.values())
    return answer


def test_all_paths_execute_and_agreement_normalizes_numeric_values():
    field, detail = run_extract(
        {
            "keyword_search": [
                Instance(
                    "8.0",
                    1,
                    "瓦斯含量8.0",
                    confidence=0.92,
                    source="keyword_search",
                    start_offset=4,
                    end_offset=7,
                )
            ],
            "llm_extract": [
                Instance(
                    "8",
                    1,
                    "瓦斯含量8.0",
                    confidence=0.85,
                    source="llm_extract",
                    start_offset=4,
                    end_offset=7,
                )
            ],
        }
    )
    assert field.extraction_method == "multipath"
    assert detail["fusion"]["status"] == "agreed"
    assert detail["validation_status"] == "passed"


def test_range_does_not_suppress_reported_fact_or_choose_another_value():
    field, detail = run_extract(
        {
            "table_lookup": [Instance("999", 1, "压力999", confidence=0.99, source="table_lookup")],
            "keyword_search": [
                Instance("8.0", 1, "瓦斯含量8.0", confidence=0.90, source="keyword_search")
            ],
        },
        validation={"min": 0, "max": 20},
    )
    assert field.field_value is None
    assert [item["value"] for item in detail["instances"]] == ["999", "8.0"]
    assert detail["instances"][0]["quality_status"] == "out_of_range"
    assert detail["rejected_candidates"] == []


def test_multiple_values_have_no_selected_main_value():
    field, detail = run_extract(
        {
            "keyword_search": [Instance("8", 1, confidence=0.95, source="keyword_search")],
            "llm_extract": [Instance("9", 1, confidence=0.99, source="llm_extract")],
        }
    )
    assert detail["validation_status"] == "multiple_values"
    assert field.field_value is None
    assert detail["fusion"]["selected"] is None
    assert len(detail["instances"]) == 2


def test_integer_type_enforced_without_validation_config():
    rule = RuleSpec(field_key="count", field_name_cn="数量", data_type="integer")
    assert RuleEngine()._validate(rule, Instance("3.5", 1))[0] == "invalid_format"


def test_non_json_model_error_is_not_a_field_value():
    rule = RuleSpec(field_key="gas", field_name_cn="瓦斯含量")
    assert parse_llm_instances("模型调用失败，请稍后重试", rule) == []


def test_keyword_does_not_steal_nearby_year_or_other_metric():
    rule = RuleSpec(
        field_key="gas",
        field_name_cn="瓦斯含量",
        data_type="float",
        strategy={"keyword_search": {"keywords": ["瓦斯含量"]}},
    )
    result = asyncio.run(
        KeywordStrategy().run(
            rule, [{"page_number": 1, "text": "瓦斯含量未测定，2026年共施工钻孔200个。"}]
        )
    )
    assert result == []


def test_regex_failure_does_not_bypass_explicit_unit_constraint():
    rule = RuleSpec(
        field_key="gas",
        field_name_cn="瓦斯含量",
        data_type="float",
        strategy={
            "keyword_search": {"keywords": ["瓦斯含量"], "regex": r"瓦斯含量[:：]?\s*(\d+)\s*m³/t"}
        },
    )
    result = asyncio.run(
        KeywordStrategy().run(rule, [{"page_number": 1, "text": "瓦斯含量：2026年报告"}])
    )
    assert result == []


def test_keyword_numeric_value_must_immediately_follow_separator():
    rule = RuleSpec(
        field_key="gas",
        field_name_cn="瓦斯含量",
        data_type="float",
        strategy={"keyword_search": {"keywords": ["瓦斯含量"]}},
    )
    assert (
        asyncio.run(
            KeywordStrategy().run(
                rule, [{"page_number": 1, "text": "瓦斯含量：未测定，2026年共施工200个钻孔"}]
            )
        )
        == []
    )
    values = asyncio.run(
        KeywordStrategy().run(rule, [{"page_number": 1, "text": "瓦斯含量：8.0 m³/t"}])
    )
    assert values[0].value == "8.0"


def test_model_quote_must_exist_and_value_must_be_a_complete_numeric_token():
    pages = [{"page_number": 2, "text": "压力为999，瓦斯含量为8.0 m³/t"}]
    assert LLMStrategy._resolve_page(pages, Instance("9", 2, "压力为999"), 2) == 0
    assert LLMStrategy._resolve_page(pages, Instance("8.0", 2, "虚构的值为8.0"), 2) == 0
    assert LLMStrategy._resolve_page(pages, Instance("8.0", 1, "瓦斯含量为8.0 m³/t"), 1) == 2


def test_llm_only_keeps_grounded_quotes_and_caps_self_reported_confidence():
    rule = RuleSpec(field_key="gas", field_name_cn="瓦斯含量")
    strategy = LLMStrategy()
    strategy._chat = AsyncMock(
        return_value='{"instances": [{"value":"8.0","page_num":2,"context":"瓦斯含量为8.0","confidence":0.99}, {"value":"999","context":"幻觉999","confidence":0.99}]}'
    )
    values = asyncio.run(strategy.run(rule, [{"page_number": 2, "text": "瓦斯含量为8.0"}]))
    assert len(values) == 1
    assert values[0].page_num == 2
    assert values[0].confidence == 0.85


def test_ambiguous_table_columns_do_not_guess_an_arbitrary_numeric_cell():
    rule = RuleSpec(
        field_key="gas",
        field_name_cn="瓦斯含量",
        data_type="float",
        strategy={"table_lookup": {"header_keywords": ["瓦斯含量"]}},
    )
    pages = [
        {
            "page_number": 1,
            "text": "",
            "tables": [{"rows": [["设计瓦斯含量", "实际瓦斯含量"], ["8.0", "9.0"]]}],
        }
    ]
    assert asyncio.run(TableStrategy().run(rule, pages)) == []


def test_fallback_is_validated_and_never_fabricates_evidence():
    engine = RuleEngine()
    rule = RuleSpec(
        field_key="gas",
        field_name_cn="瓦斯含量",
        data_type="float",
        fallback_value="999",
        validation={"max": 20},
    )
    assert asyncio.run(engine.extract_one([], rule))[0] is None
    rule.fallback_value = "8.0"
    field, detail = asyncio.run(engine.extract_one([], rule))
    assert field is None and detail["configured_fallback"] == "8.0"
    assert detail["validation_status"] == "missing"


def test_ocr_zero_confidence_is_preserved_and_https_is_preserved():
    assert PaddleOCRClient._normalize_base("https://example.test/ocr") == "https://example.test"
    text, confidence, _ = PaddleOCRClient._parse_item({"text": "8.0", "confidence": 0}, 0)
    assert text == "8.0" and confidence == 0
