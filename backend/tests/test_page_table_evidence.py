import json
from copy import deepcopy

import pytest

from app.rules.spec import RuleSpec
from app.services.page_table_evidence import table_fact_support

HEADER = "实测瓦斯含量(m3/t)"
ROWS = [["测点", HEADER, "实测瓦斯压力(MPa)"], ["测点1", "3.42", "0.23"], ["测点10", "4.91", "0.36"]]
RULE = RuleSpec(field_key="gas", field_name_cn="瓦斯含量", data_type="float", unit="m3/t",
                value_kind="measured", dimensions=["sample"])


def decision():
    return {"value": "3.42", "unit": "m3/t", "value_kind": "measured", "dimensions": {"sample": "测点1"},
        "table_relation": {"row_index": 1, "column_index": 1, "column_header": HEADER,
            "row_quote": "测点1 3.42 0.23"}}


def html_unit():
    return {"text": "<table>" + "".join("<tr>" + "".join(f"<td>{cell}</td>" for cell in row)
        + "</tr>" for row in ROWS) + "</table>"}


def test_html_table_proves_exact_cell_unit_kind_and_identity():
    result = table_fact_support(html_unit(), decision(), RULE)
    assert result["usable"]
    assert set(result["attributes"]) == {"value", "unit", "value_kind", "table_relation", "dimensions.sample"}
    assert result["attributes"]["unit"]["basis"] == "table_header_binding"


@pytest.mark.parametrize("patch", [
    {"value": "4.91"},
    {"value": "0.23"},
    {"unit": "MPa"},
    {"dimensions": {"sample": "测点10"}},
    {"table_relation": {"row_index": 2, "column_index": 1, "column_header": HEADER, "row_quote": "测点1 3.42 0.23"}},
    {"table_relation": {"row_index": 1, "column_index": 2, "column_header": HEADER, "row_quote": "测点1 3.42 0.23"}},
    {"table_relation": {"row_index": 1, "column_index": 1, "column_header": HEADER}},
])
def test_wrong_row_column_identity_unit_and_incomplete_relation_are_rejected(patch):
    fact = {**decision(), **deepcopy(patch)}
    assert not table_fact_support(html_unit(), fact, RULE)["usable"]


def test_correct_neighbor_column_cannot_satisfy_wrong_field_semantics():
    fact = decision()
    fact.update(value="0.23", unit="MPa")
    fact["table_relation"].update(column_index=2, column_header=ROWS[0][2])
    result = table_fact_support(html_unit(), fact, RULE)
    assert "TABLE_FIELD_COLUMN_MISMATCH" in result["issues"]


def test_json_line_table_and_normalized_superscript_unit():
    records = [{"table_id": "t1", "header": ROWS[0]},
               *[{"row_index": i, "row": row} for i, row in enumerate(ROWS[1:])]]
    unit = {"text": "\n".join(json.dumps(record, ensure_ascii=False) for record in records)}
    fact = decision()
    fact["unit"] = "m³/t"
    assert table_fact_support(unit, fact, RULE)["usable"]


def test_structured_plain_transcription_supports_grid_but_prose_does_not():
    unit = {"text": "OCR text", "transcription": "\n".join("\t".join(row) for row in ROWS)}
    assert table_fact_support(unit, decision(), RULE)["usable"]
    unit["transcription"] = " ".join(" ".join(row) for row in ROWS)
    assert not table_fact_support(unit, decision(), RULE)["usable"]


def test_merged_cells_and_noninteger_coordinates_remain_unknown():
    unit = html_unit()
    unit["text"] = unit["text"].replace("<td>测点</td>", '<td rowspan="2">测点</td>')
    assert not table_fact_support(unit, decision(), RULE)["usable"]
    fact = decision()
    fact["table_relation"]["row_index"] = 1.5
    assert not table_fact_support(html_unit(), fact, RULE)["usable"]


def test_unknown_kind_is_never_verified_from_table():
    fact = decision()
    fact["value_kind"] = "unknown"
    result = table_fact_support(html_unit(), fact, RULE)
    assert not result["usable"]
    assert "value_kind" not in result["attributes"]


def test_reported_literal_kind_does_not_assert_measurement_semantics():
    fact = decision()
    fact["value_kind"] = "reported"
    assert table_fact_support(html_unit(), fact, RULE)["attributes"]["value_kind"]["value"] == "reported"
