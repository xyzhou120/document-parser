from types import SimpleNamespace

from app.models.contracts import TextBlock
from app.services.content_build_service import _jsonable
from app.services.target_contract import attach_target_payload, normalize_decision, structured_target_rejection
from app.services.page_table_evidence import table_row_support


def table_rule():
    return SimpleNamespace(target_type="table", output_schema={
        "row_identity": ["point"],
        "columns": [
            {"key": "point", "header": "测点", "required": True},
            {"key": "pressure", "header": "瓦斯压力", "data_type": "float", "unit": "MPa", "required": True},
        ],
    }, field_name_cn="测点表", data_type="float", unit="MPa", value_kind="measured",
    strategy={"agent_extract": {"identity_dimensions": ["point"]}})


def test_statement_keeps_polarity_and_text_without_business_judgement():
    rule = SimpleNamespace(target_type="statement")
    decision = normalize_decision({"statement": {"text": "未建立瓦斯抽采系统", "polarity": "negated"}}, rule)
    fact = {"value": decision["value"], "quote": decision["quote"], "evidence_ids": ["e1"]}
    attach_target_payload(fact, decision, rule)
    assert fact["target_payload"]["statement"]["polarity"] == "negated"
    assert fact["target_payload"]["statement"]["text"] == "未建立瓦斯抽采系统"


def test_table_row_support_binds_configured_columns_to_one_row():
    unit = {"text": '{"header":["测点","瓦斯压力"],"rows":[["A-1","1.23"]]}', "table": True,
            "tables": [{"header": ["测点", "瓦斯压力"], "rows": [["A-1", "1.23"]]}]}
    decision = {"value": "1.23", "table_relation": {
        "row_index": 1, "column_index": 1, "column_header": "瓦斯压力", "row_quote": "A-1 1.23"}}
    result = table_row_support(unit, decision, table_rule())
    assert result["usable"] is True
    assert result["row"]["identity"]["point"] == "A-1"
    assert result["row"]["cells"]["pressure"]["value"] == "1.23"


def test_unbound_table_row_is_not_marked_deliverable():
    rule = SimpleNamespace(target_type="table")
    fact = {"value": "1.23", "row_status": "REVIEW_REQUIRED", "evidence_ids": ["e1"]}
    attach_target_payload(fact, {"value": "1.23"}, rule)
    assert fact["row_status"] == "REVIEW_REQUIRED"
    assert fact["target_payload"]["row"]["status"] == "REVIEW_REQUIRED"


def test_structured_targets_only_run_on_page_batch():
    rules = [SimpleNamespace(target_type="table"), SimpleNamespace(target_type="fact")]
    assert structured_target_rejection(rules, "checklist_agent", "page_batch_v1")
    assert structured_target_rejection(rules, "reliable", "field_retrieval_v2")
    assert structured_target_rejection(rules, "reliable", "page_batch_v1") is None
    assert structured_target_rejection([SimpleNamespace(target_type="fact")], "rules", None) is None


def test_checkpoint_serializes_ocr_text_blocks():
    stored = _jsonable({"prefetched_ocr": {"p2:page": [TextBlock(text="钻孔45个", confidence=0.9)]}})
    assert stored["prefetched_ocr"]["p2:page"][0]["text"] == "钻孔45个"
