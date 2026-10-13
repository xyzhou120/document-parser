"""离线真实文档选择、人工标注和提取评估。

不发起远程调用，也不生成真实值。请从仓库根目录执行：
backend/.venv/Scripts/python.exe backend/scripts/agent_evaluation.py --help
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import unicodedata
from collections import Counter
from decimal import Decimal
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
EVIDENCE_ROLES = {"value", "unit", "column_header", "row_header", "scope", "object", "source", "date"}
DIMENSION_KEYS = {"workface", "evaluation_unit", "measurement_point", "roadway", "measurement_date", "scope_level"}
KNOWN_NOTES = {
    0: "人工核对设计/实际/补孔/效果检验孔数；既有诊断提示，不能作为金标准。",
    100: "人工核对总孔数和评价单元孔数、原始与计算瓦斯含量；不能直接采用诊断值。",
    131: "扫描报告单；重点原图核对数字、上标单位、评价单元。OCR 曾混淆 m³/t 与 m²/t。",
    328: "人工核对设计孔数与工程量米数，避免把长度作为数量。",
    460: "人工核对不同评价单元、设计/实际孔数与钻场孔数。",
}
DEFAULT_FIELDS = [
    # 首轮字段有意覆盖数量、维度和取值类型；操作员可通过 --fields
    # 选择其他知识包修订的字段。
    "designed_hole_count", "actual_hole_count", "supplementary_hole_count",
    "measurement_point_count", "desorbable_gas_content_measured",
    "desorbable_gas_content_calculated", "evaluation_unit_range",
]


def read_json(path: str | Path) -> Any:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def write_json(path: str | Path, data: Any) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def layout(row: dict) -> str:
    pages = row.get("pages", 0)
    zero = row.get("zero_text_pages", 0)
    if pages and zero == pages:
        return "scan"
    if zero or row.get("ocr_gate_pages", 0):
        return "mixed"
    return "native"


def select_samples(inventory: list[dict], count: int = 26, seed: str = "agent-v1") -> dict:
    """按文件哈希去重；由重复项关联的矿井不能横跨开发集和留出集。"""
    if count < 2:
        raise ValueError("count must be at least 2")
    by_hash: dict[str, list[dict]] = {}
    exclusions = []
    for row in inventory:
        if row.get("status") != "pdf" or not row.get("pages") or not row.get("sha256"):
            exclusions.append({"id": row.get("id"), "path": row.get("path"), "reason": "not_readable_pdf"})
            continue
        by_hash.setdefault(row["sha256"], []).append(row)
    parent: dict[str, str] = {}

    def find(mine: str) -> str:
        parent.setdefault(mine, mine)
        if parent[mine] != mine:
            parent[mine] = find(parent[mine])
        return parent[mine]

    for duplicates in by_hash.values():
        mines = sorted({r["mine"] for r in duplicates})
        for mine in mines:
            parent[find(mine)] = find(mines[0])
    components: dict[str, list[str]] = {}
    for mine in parent:
        components.setdefault(find(mine), []).append(mine)
    group_for = {m: "|".join(sorted(ms)) for ms in components.values() for m in ms}
    known_groups = {
        group_for[r["mine"]] for rs in by_hash.values() for r in rs if r["id"] in KNOWN_NOTES
    }
    candidates = []
    for _file_hash, duplicates in by_hash.items():
        row = min(duplicates, key=lambda r: (r["id"] not in KNOWN_NOTES, r["id"]))
        group = group_for[row["mine"]]
        digest = hashlib.sha256((seed + group).encode()).hexdigest()
        split = "dev" if group in known_groups or int(digest[:8], 16) % 4 else "holdout"
        candidates.append({
            **{k: row.get(k) for k in ("id", "mine", "path", "sha256", "bytes", "pages", "zero_text_pages", "ocr_gate_pages")},
            "layout": layout(row), "split": split, "mine_group": group,
            "duplicate_paths": sorted(r["path"] for r in duplicates if r["path"] != row["path"]),
            "source_mines": sorted({r["mine"] for r in duplicates}),
            "sampling_notes": KNOWN_NOTES.get(row["id"], "人工核对原件，模板尚未标注。"),
            "large_file": row.get("bytes", 0) > 50 * 1024 * 1024,
            "long_document": row.get("pages", 0) >= 100,
        })
    candidates.sort(key=lambda r: hashlib.sha256((seed + r["sha256"]).encode()).hexdigest())
    chosen: list[dict] = []

    def take(predicate, quota: int) -> None:
        for row in candidates:
            if len(chosen) >= count or quota <= 0:
                return
            if row not in chosen and predicate(row):
                chosen.append(row)
                quota -= 1

    take(lambda r: r["id"] in KNOWN_NOTES, len(KNOWN_NOTES))
    for split in ("holdout", "dev"):
        for kind in ("scan", "mixed", "native"):
            take(lambda r, s=split, k=kind: r["split"] == s and r["layout"] == k, 2)
    take(lambda r: r["large_file"], 1)
    take(lambda r: r["long_document"], 1)
    # 优先加入新矿井，再增加已选矿井的报告。
    for row in candidates:
        if len(chosen) >= count:
            break
        if row["mine_group"] not in {r["mine_group"] for r in chosen}:
            chosen.append(row)
    take(lambda r: True, count)
    chosen.sort(key=lambda r: (r["split"], r["mine"], r["id"]))
    return {
        "schema_version": 1, "seed": seed, "checklist_type": "gas_report_agent",
        "selection_policy": "sha256 dedup + duplicate-connected mine split; known inspected examples dev only",
        "annotation_status": "NOT_ANNOTATED", "documents": chosen, "exclusions": exclusions,
        "summary": {"selected": len(chosen), "requested": count,
                    "split": dict(Counter(r["split"] for r in chosen)),
                    "layout": dict(Counter(r["layout"] for r in chosen)),
                    "unique_readable_pdfs": len(by_hash), "excluded": len(exclusions)},
    }


def annotation_template(manifest: dict, field_keys: list[str]) -> dict:
    return {
        "schema_version": 1, "checklist_type": manifest["checklist_type"],
        "instructions": "必须从原图独立标注并复核；facts 默认空不表示缺失；参考文档方案。",
        "documents": [{
            "id": d["id"], "sha256": d["sha256"], "path": d["path"],
            "pages": d["pages"], "split": d["split"], "sampling_notes": d["sampling_notes"],
            **{k: d[k] for k in ("copied_file", "mine", "mine_group", "layout", "bytes", "page_bounds") if k in d},
            "fields": [{
                "field_key": key, "status": "NOT_ANNOTATED", "scope": "whole_document",
                "scope_pages": [], "complete": False, "annotator": "", "adjudicator": "",
                "verified_at": "", "facts": [], "unreadable_evidence": [], "notes": "",
            } for key in field_keys],
        } for d in manifest["documents"]],
        "fact_example_do_not_copy": {
            "raw_value": "原图上的原始值", "normalized_value": "经明确规则归一的值",
            "unit": "原图单位或null", "dimensions": {"workface": "原图对象"},
            "value_kind": "actual", "source_role": "report_body",
            "evidence": [{"page_number": 1, "bbox": [0, 0, 100, 100],
                          "coordinate_system": "PDF_POINT", "quote": "原图文字",
                          "role": "value", "table_id": None}],
        },
    }


def normalize_value(value: Any) -> str:
    text = unicodedata.normalize("NFKC", str(value)).strip()
    if re.fullmatch(r"[+-]?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?", text):
        number = Decimal(text.replace(",", ""))
        return "0" if number == 0 else str(number.normalize())
    if re.fullmatch(r"[+-]?(?:\d+\.\d*|\.\d+|\d+)(?:[eE][+-]?\d+)?", text):
        number = Decimal(text)
        return "0" if number == 0 else str(number.normalize())
    return text


def normalize_unit(value: Any) -> str | None:
    if value is None:
        return None
    # 只规范化排版，不擅自把 m²/t 修成 m³/t，也不转换量级。
    return unicodedata.normalize("NFKC", str(value)).replace(" ", "").replace("^", "")


def normalized_dimensions(fact: dict) -> tuple:
    return tuple(sorted((k, normalize_value(v)) for k, v in fact.get("dimensions", {}).items()))


def value_key(fact: dict) -> str:
    return normalize_value(fact.get("normalized_value", fact.get("raw_value", "")))


def fact_key(fact: dict) -> tuple:
    return (value_key(fact), normalize_unit(fact.get("unit")), normalized_dimensions(fact),
            fact.get("value_kind", "unknown"), fact.get("source_role", "unknown"))


def _page_bounds(doc: dict, source_root: Path | None) -> dict[int, tuple[float, float]]:
    bounds = {}
    for page in doc.get("page_bounds", []):
        number = page.get("page_number")
        if (isinstance(number, bool) or not isinstance(number, int) or not 1 <= number <= doc.get("pages", 0)
                or not all(not isinstance(page.get(k), bool) and isinstance(page.get(k), int | float)
                           and math.isfinite(page[k]) and page[k] > 0 for k in ("width", "height"))):
            raise ValueError("invalid physical page bounds")
        if number in bounds:
            raise ValueError("duplicate physical page bounds")
        bounds[number] = page["width"], page["height"]
    if source_root is not None and doc.get("copied_file"):
        path = (source_root / doc["copied_file"]).resolve()
        if not path.is_relative_to(source_root.resolve()):
            raise ValueError("copied_file escapes sample root")
        if not path.is_file():
            raise ValueError(f"Sample file missing: {path}")
        import pymupdf

        raw = path.read_bytes()
        if hashlib.sha256(raw).hexdigest() != doc.get("sha256"):
            raise ValueError("Source hash mismatch")
        with pymupdf.open(stream=raw, filetype="pdf") as pdf:
            if len(pdf) != doc.get("pages"):
                raise ValueError("Source page count mismatch")
            bounds = {i + 1: (page.rect.width, page.rect.height) for i, page in enumerate(pdf)}
    return bounds


def validate_annotations(gold: dict, source_root: Path | None = None) -> list[str]:
    errors = []
    seen_docs = set()
    seen_hashes = set()
    for doc in gold.get("documents", []):
        label = str(doc.get("id"))
        try:
            bounds = _page_bounds(doc, source_root)
        except (ValueError, TypeError, KeyError, OSError) as exc:
            errors.append(f"{label}: {exc}")
            bounds = {}
        if doc.get("id") in seen_docs or doc.get("sha256") in seen_hashes:
            errors.append(f"{label}: duplicate document id/hash")
        seen_docs.add(doc.get("id"))
        seen_hashes.add(doc.get("sha256"))
        if not re.fullmatch(r"[0-9a-f]{64}", doc.get("sha256", "")):
            errors.append(f"{label}: invalid sha256")
        keys = set()
        for field in doc.get("fields", []):
            label = f"{doc.get('id')}:{field.get('field_key')}"
            if not field.get("field_key") or field["field_key"] in keys:
                errors.append(f"{label}: missing/duplicate field_key")
            keys.add(field.get("field_key"))
            status = field.get("status")
            if status not in {"NOT_ANNOTATED", "DRAFT", "VERIFIED", "ABSENT", "UNREADABLE"}:
                errors.append(f"{label}: invalid status")
            if field.get("scope") not in {"whole_document", "sampled_pages"}:
                errors.append(f"{label}: invalid scope")
            if field.get("scope") == "sampled_pages" and not field.get("scope_pages"):
                errors.append(f"{label}: sampled_pages requires scope_pages")
            pages = field.get("scope_pages", [])
            if any(isinstance(p, bool) or not isinstance(p, int) or p < 1 or p > doc.get("pages", 0) for p in pages):
                errors.append(f"{label}: invalid scope_pages")
            if status in {"VERIFIED", "ABSENT"}:
                if not field.get("complete"):
                    errors.append(f"{label}: scored scope must be completely annotated")
                if not field.get("annotator") or not field.get("adjudicator") or not field.get("verified_at"):
                    errors.append(f"{label}: needs annotator, adjudicator and verified_at")
                elif field["annotator"] == field["adjudicator"]:
                    errors.append(f"{label}: independent adjudicator required")
            if status == "VERIFIED" and not field.get("facts"):
                errors.append(f"{label}: VERIFIED needs facts; absence uses ABSENT")
            if status == "ABSENT" and field.get("facts"):
                errors.append(f"{label}: ABSENT cannot contain facts")
            group_keys = set()
            for fact in field.get("facts", []):
                if not isinstance(fact.get("dimensions"), dict):
                    errors.append(f"{label}: dimensions must be explicit object (empty allowed for document scope)")
                    continue
                for name in ("raw_value", "normalized_value", "unit", "value_kind", "source_role"):
                    if name not in fact:
                        errors.append(f"{label}: fact missing {name}")
                if any(isinstance(fact.get(name), float) and not math.isfinite(fact[name])
                       for name in ("raw_value", "normalized_value")):
                    errors.append(f"{label}: fact value must be finite")
                if status == "VERIFIED" and (
                    fact.get("value_kind") in {None, "unknown"}
                    or fact.get("source_role") in {None, "unknown"}
                    or any(v in (None, "", "unknown") for v in fact["dimensions"].values())
                ):
                    errors.append(f"{label}: verified fact has unknown identity/type/source")
                if status == "VERIFIED" and set(fact["dimensions"]) - DIMENSION_KEYS:
                    errors.append(f"{label}: unknown dimension keys: {sorted(set(fact['dimensions']) - DIMENSION_KEYS)}")
                key = fact_key(fact)
                if key in group_keys:
                    errors.append(f"{label}: duplicate fact group; merge evidence")
                group_keys.add(key)
                if status == "VERIFIED" and not fact.get("evidence"):
                    errors.append(f"{label}: verified fact needs original-image evidence")
                for ev_index, ev in enumerate(fact.get("evidence", [])):
                    evidence_label = f"{label}:evidence[{ev_index}]"
                    page = ev.get("page_number")
                    if isinstance(page, bool) or not isinstance(page, int) or not 1 <= page <= doc.get("pages", 0):
                        errors.append(f"{evidence_label}: invalid evidence page")
                    if field.get("scope") == "sampled_pages" and page not in pages:
                        errors.append(f"{label}: evidence outside annotated scope")
                    box = ev.get("bbox")
                    valid_box = (isinstance(box, list) and len(box) == 4
                                 and all(not isinstance(v, bool) and isinstance(v, int | float) and math.isfinite(v) for v in box)
                                 and box[0] >= 0 and box[1] >= 0 and box[2] > box[0] and box[3] > box[1])
                    if not valid_box:
                        errors.append(f"{evidence_label}: evidence needs finite, nonnegative bbox with positive area")
                    elif ev.get("coordinate_system") == "PDF_POINT" and page in bounds:
                        if box[2] > bounds[page][0] or box[3] > bounds[page][1]:
                            errors.append(f"{evidence_label}: bbox outside physical page bounds")
                    if ev.get("coordinate_system") not in {"PDF_POINT", "PIXEL", "NORMALIZED"}:
                        errors.append(f"{label}: invalid coordinate_system")
                    if status == "VERIFIED" and ev.get("coordinate_system") != "PDF_POINT":
                        errors.append(f"{evidence_label}: VERIFIED evidence requires PDF_POINT")
                    if status == "VERIFIED" and ev.get("role") not in EVIDENCE_ROLES:
                        errors.append(f"{evidence_label}: verified evidence needs known explicit role")
    return errors


def iou(a: list, b: list) -> float:
    intersection = max(0, min(a[2], b[2]) - max(a[0], b[0])) * max(0, min(a[3], b[3]) - max(a[1], b[1]))
    union = (a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - intersection
    return intersection / union if union > 0 else 0.0


def evidence_matches(pred: dict, gold: dict, threshold: float | None) -> bool:
    supported_roles = set()
    required_roles = {e.get("role") for e in gold.get("evidence", [])}
    for p in pred.get("evidence", []):
        for g in gold.get("evidence", []):
            if p.get("page_number") != g.get("page_number") or p.get("role") != g.get("role"):
                continue
            if p.get("coordinate_system") != "PDF_POINT" or g.get("coordinate_system") != "PDF_POINT":
                continue
            if threshold is None:
                supported_roles.add(g.get("role"))
                continue
            boxes = [p.get("bbox"), g.get("bbox")]
            if (all(isinstance(b, list) and len(b) == 4 and all(not isinstance(v, bool) and isinstance(v, int | float)
                    and math.isfinite(v) for v in b) and b[2] > b[0] and b[3] > b[1] for b in boxes)
                    and iou(p["bbox"], g["bbox"]) >= threshold):
                supported_roles.add(g.get("role"))
    return bool(required_roles) and required_roles <= supported_roles


def unique_facts(facts: list[dict]) -> list[dict]:
    groups: dict[tuple, dict] = {}
    for fact in facts:
        key = fact_key(fact)
        if key not in groups:
            groups[key] = {**fact, "evidence": list(fact.get("evidence", []))}
        else:
            groups[key]["evidence"].extend(fact.get("evidence", []))
    return list(groups.values())


def ratio(a: int, b: int) -> float | None:
    return a / b if b else None


def evaluate(gold: dict, predictions: dict, evidence_iou: float | None = None,
             manual_work: list[dict] | None = None) -> dict:
    errors = validate_annotations(gold)
    if errors:
        raise ValueError("Invalid annotations: " + "; ".join(errors))
    predicted = {str(d["id"]): d for d in predictions.get("documents", [])}
    if len(predicted) != len(predictions.get("documents", [])):
        raise ValueError("Duplicate prediction document ids")
    totals: Counter = Counter()
    rows = []
    strata: dict[tuple[str, str], Counter] = {}
    stratum_documents: dict[tuple[str, str], set] = {}
    document_statuses: Counter = Counter()
    for doc in gold.get("documents", []):
        pred_doc = predicted.get(str(doc["id"]), {})
        document_statuses[pred_doc.get("status", "PRESENT" if pred_doc else "MISSING")] += 1
        if pred_doc and pred_doc.get("sha256") != doc["sha256"]:
            raise ValueError(f"Prediction file hash mismatch: {doc['id']}")
        pred_fields = {f["field_key"]: f for f in pred_doc.get("fields", [])}
        if len(pred_fields) != len(pred_doc.get("fields", [])):
            raise ValueError(f"Duplicate prediction field keys: {doc['id']}")
        for field in doc.get("fields", []):
            stratum_names = {
                "split": str(doc.get("split", "unknown")),
                "layout": str(doc.get("layout", "unknown")),
                "mine_group": str(doc.get("mine_group", doc.get("mine", "unknown"))),
                "field_key": field["field_key"],
                "pages": "long" if doc.get("pages", 0) >= 100 else "short",
                "bytes": ("large" if doc["bytes"] >= 50 * 1024 * 1024 else "small") if doc.get("bytes") is not None else "unknown",
            }
            for dimension, value in stratum_names.items():
                key = dimension, value
                strata.setdefault(key, Counter())["annotation_fields"] += 1
                stratum_documents.setdefault(key, set()).add(str(doc["id"]))
            if field["status"] not in {"VERIFIED", "ABSENT"}:
                totals["unscored_fields"] += 1
                for dimension, value in stratum_names.items():
                    strata[dimension, value]["unscored_fields"] += 1
                continue
            totals["scored_fields"] += 1
            totals["whole_document_fields" if field["scope"] == "whole_document" else "sampled_scope_fields"] += 1
            ps = unique_facts(pred_fields.get(field["field_key"], {}).get("facts", []))
            outside = []
            if field["scope"] == "sampled_pages":
                outside = [p for p in ps if p.get("evidence") and all(
                    e.get("page_number") not in field["scope_pages"] for e in p["evidence"])]
                ps = [p for p in ps if p not in outside]
            gs = unique_facts(field.get("facts", []))
            unmatched = list(range(len(gs)))
            matches = []
            failures = []
            for p in ps:
                match = next((j for j in unmatched if fact_key(p) == fact_key(gs[j])), None)
                if match is None:
                    failures.append(p)
                    continue
                unmatched.remove(match)
                matches.append((p, gs[match]))
            tp, fp, fn = len(matches), len(failures), len(unmatched)
            supported = sum(evidence_matches(p, g, evidence_iou) for p, g in matches)
            totals.update({"tp": tp, "fp": fp, "fn": fn, "evidence_supported_tp": supported,
                           "gold_facts": len(gs), "predicted_facts": len(ps), "outside_scope_predictions": len(outside)})
            # 仅值匹配的配对用于诊断单位、身份或类型错误，不计为真正例。
            diagnostic_gold = list(range(len(gs)))
            for p in ps:
                j = next((j for j in diagnostic_gold if value_key(p) == value_key(gs[j])), None)
                if j is not None:
                    diagnostic_gold.remove(j)
                    totals["value_matched_pairs"] += 1
                    totals["unit_correct_pairs"] += normalize_unit(p.get("unit")) == normalize_unit(gs[j].get("unit"))
                    totals["binding_correct_pairs"] += normalized_dimensions(p) == normalized_dimensions(gs[j])
                    totals["type_correct_pairs"] += p.get("value_kind") == gs[j].get("value_kind")
                    totals["source_correct_pairs"] += p.get("source_role") == gs[j].get("source_role")
            rows.append({"document_id": doc["id"], "field_key": field["field_key"],
                         "scope": field["scope"], "tp": tp, "fp": fp, "fn": fn,
                         "evidence_supported_tp": supported, "false_positive": failures,
                         "false_negative": [gs[j] for j in unmatched]})
            for dimension, value in stratum_names.items():
                counter = strata.setdefault((dimension, value), Counter())
                stratum_documents.setdefault((dimension, value), set()).add(str(doc["id"]))
                counter.update({"fields": 1, "tp": tp, "fp": fp, "fn": fn,
                                "gold_facts": len(gs), "predicted_facts": len(ps),
                                "evidence_supported_tp": supported})
    summary = {**dict(totals), "precision": ratio(totals["tp"], totals["tp"] + totals["fp"]),
               "recall": ratio(totals["tp"], totals["tp"] + totals["fn"]),
               "evidence_support_rate": ratio(totals["evidence_supported_tp"], totals["tp"]),
               "supported_precision": ratio(totals["evidence_supported_tp"], totals["tp"] + totals["fp"]),
               "unit_accuracy_on_value_pairs": ratio(totals["unit_correct_pairs"], totals["value_matched_pairs"]),
               "binding_accuracy_on_value_pairs": ratio(totals["binding_correct_pairs"], totals["value_matched_pairs"]),
               "type_accuracy_on_value_pairs": ratio(totals["type_correct_pairs"], totals["value_matched_pairs"]),
               "source_accuracy_on_value_pairs": ratio(totals["source_correct_pairs"], totals["value_matched_pairs"]),
               "averaging": "micro over facts"}
    stratified = []
    for (dimension, value), counter in sorted(strata.items()):
        stratified.append({"dimension": dimension, "value": value, **dict(counter),
                           "documents": len(stratum_documents[dimension, value]), "averaging": "micro",
                           "precision": ratio(counter["tp"], counter["tp"] + counter["fp"]),
                           "recall": ratio(counter["tp"], counter["tp"] + counter["fn"]),
                           "evidence_support_rate": ratio(counter["evidence_supported_tp"], counter["tp"])})
    warnings = ["These sample strata describe evaluated inputs only, not all mines or checklists."]
    if totals["sampled_scope_fields"]:
        warnings.append("Sampled-page annotation cannot establish whole-document recall.")
    if any(len(ids) < 5 for ids in stratum_documents.values()):
        warnings.append("Some strata contain fewer than 5 documents; do not claim production generalisation.")
    if any(not d.get("page_bounds") for d in gold.get("documents", [])):
        warnings.append("Physical page bounds were unavailable for some annotations; validate with --sample-root or page_bounds.")
    return {"schema_version": 1, "status": "EVALUATED" if totals["scored_fields"] else "NO_VERIFIED_GOLD",
            "evidence_mode": "page_and_role" if evidence_iou is None else f"bbox_iou>={evidence_iou}",
            "scope_warning": "sampled_pages scores cannot establish whole-document recall; null metrics are undefined",
            "summary": summary, "stratified": stratified, "details": rows,
            "scope_warnings": warnings,
            "document_outcomes": {"total_documents": len(gold.get("documents", [])),
                                  "status_counts": dict(document_statuses),
                                  "prediction_present_rate": ratio(len(gold.get("documents", [])) - document_statuses["MISSING"], len(gold.get("documents", [])))},
            "manual_cost": manual_cost_report(manual_work if manual_work is not None else gold.get("manual_work", [])),
            "unscored_prediction_documents": sorted(set(predicted) - {str(d["id"]) for d in gold.get("documents", [])}),
            "run_metadata": predictions.get("run_metadata", {})}


def manual_cost_report(records: list[dict]) -> dict:
    """只统计明确的实际工作秒数，不统计浏览器停留时间。"""
    by_phase: dict[str, dict] = {}
    allowed = {"pilot", "annotation", "review", "adjudication", "correction"}
    for index, row in enumerate(records):
        seconds = row.get("active_seconds")
        if (row.get("phase") not in allowed or isinstance(seconds, bool)
                or not isinstance(seconds, int | float) or not math.isfinite(seconds) or seconds < 0):
            raise ValueError(f"manual_work[{index}]: invalid phase/active_seconds")
        phase = by_phase.setdefault(row["phase"], {"active_seconds": 0, "documents": set(), "facts": 0,
                                                    "reviewed_facts": 0, "corrected_facts": 0, "rejected_facts": 0})
        phase["active_seconds"] += seconds
        phase["documents"].add(str(row["document_id"]))
        for key in ("facts", "reviewed_facts", "corrected_facts", "rejected_facts"):
            count = row.get(key, 0)
            if isinstance(count, bool) or not isinstance(count, int) or count < 0:
                raise ValueError(f"manual_work[{index}]: invalid {key}")
            phase[key] += count
        if row.get("corrected_facts", 0) + row.get("rejected_facts", 0) > row.get("reviewed_facts", 0):
            raise ValueError(f"manual_work[{index}]: dispositions exceed reviewed facts")
    phases = []
    for phase, counts in sorted(by_phase.items()):
        minutes = counts["active_seconds"] / 60
        phases.append({"phase": phase, **{k: v for k, v in counts.items() if k != "documents"},
                       "documents": len(counts["documents"]), "minutes": minutes,
                       "minutes_per_document": ratio(minutes, len(counts["documents"])),
                       "minutes_per_fact": ratio(minutes, counts["facts"]),
                       "correction_rate": ratio(counts["corrected_facts"], counts["reviewed_facts"]),
                       "rejection_rate": ratio(counts["rejected_facts"], counts["reviewed_facts"])})
    return {"status": "RECORDED" if records else "NOT_RECORDED", "records": len(records), "phases": phases}


def import_results(manifest: dict, document_id: int, payload: dict) -> dict:
    doc = next((d for d in manifest["documents"] if d["id"] == document_id), None)
    if doc is None:
        raise ValueError("document id is not in manifest")
    if payload.get("file_hash") != doc["sha256"]:
        raise ValueError("API export must include matching file_hash; manifest alone cannot prove identity")
    # 从解析详情 API 导出；不能把标量回退值转换为来源事实。
    details = payload.get("details", payload.get("field_details", []))
    if not details:
        details = [{"field_key": f["field_key"], **(f.get("extraction_detail") or {})}
                   for f in payload.get("fields", [])]
    fields = []
    for detail in details:
        facts = []
        registry = detail.get("evidence_registry", {})
        instances = detail.get("instances", []) or detail.get("value_groups", [])
        for inst in instances:
            value = inst.get("value", inst.get("field_value"))
            if value is None:
                continue
            ids = inst.get("evidence_ids", inst.get("evidence_refs", []))
            dimensions_raw = inst.get("dimensions", inst.get("identity", {}))
            dimensions_raw = dimensions_raw if isinstance(dimensions_raw, dict) else {}
            dimension_ids = [
                ref
                for value in dimensions_raw.values()
                if isinstance(value, dict)
                for ref in value.get("evidence_ids", [])
                if isinstance(ref, str)
            ]
            ids = list(dict.fromkeys([ref for ref in ids if isinstance(ref, str)] + dimension_ids))
            evidence = inst.get("evidence", [])
            if isinstance(evidence, dict):
                evidence = [evidence]
            evidence = [dict(ev) for ev in evidence]
            # 代理的 evidence_roles 是语义角色的唯一依据。
            # 一个注册位置可能支持多个角色，因此每个明确角色保留一条证据记录；
            # 未知角色保持未知。
            role_map = inst.get("evidence_roles", {})
            if not isinstance(role_map, dict):
                role_map = {}
            ids = list(dict.fromkeys(ids + [ref for ref in role_map if isinstance(ref, str)]))
            if registry and ids and (role_map or not evidence):
                evidence = []
                for eid in ids:
                    base = registry.get(eid)
                    if not isinstance(base, dict):
                        continue
                    roles = role_map.get(eid, [base.get("role", "unknown")])
                    if isinstance(roles, str):
                        roles = [roles]
                    if not isinstance(roles, list) or not roles:
                        roles = ["unknown"]
                    for role in roles:
                        # 不根据字段名或维度名推断角色。
                        role_name = str(role) if role not in (None, "") else "unknown"
                        evidence.append({**base, "role": role_name})
            for ev in evidence:
                if isinstance(ev.get("bbox"), dict):
                    ev["bbox"] = [ev["bbox"][k] for k in ("x0", "y0", "x1", "y1")]
            if not evidence and inst.get("page_num"):
                box = inst.get("bbox")
                if isinstance(box, dict):
                    box = [box[k] for k in ("x0", "y0", "x1", "y1")]
                evidence = [{"page_number": inst["page_num"], "bbox": box,
                             "coordinate_system": inst.get("coordinate_system"),
                             "role": inst.get("evidence_role", "unknown"),
                             "quote": inst.get("source_text", inst.get("context", ""))}]
            dimensions = {k: v.get("value") if isinstance(v, dict) else v
                          for k, v in dimensions_raw.items()}
            facts.append({"raw_value": value, "normalized_value": value,
                          "unit": inst.get("unit"), "dimensions": dimensions,
                          "value_kind": inst.get("value_kind", "unknown"),
                          "source_role": inst.get("source_role", "unknown"), "evidence": evidence})
        fields.append({"field_key": detail["field_key"], "facts": unique_facts(facts)})
    return {"schema_version": 1, "documents": [{"id": document_id, "sha256": doc["sha256"], "fields": fields,
                                                 "status": payload.get("status", "UNKNOWN")}],
            "run_metadata": {**payload.get("run_metadata", {}), "coverage": payload.get("agent_runtime", {}).get("coverage"),
                             "source": "parse_detail_export", "parse_run_id": payload.get("parse_run_id"),
                             "compatibility_warnings": (["Legacy result schema or missing run identity; not a frozen formal prediction"]
                                                       if payload.get("schema_version") != 1 or not (payload.get("run_metadata", {}).get("run_id") or payload.get("parse_run_id")) else []),
                             "limitations": "Missing dimensions/source_role remain unknown; never infer from gold."}}


def stability(runs: list[dict]) -> dict:
    frozen = [r.get("run_metadata", {}).get("frozen_configuration") for r in runs]
    known = [f for f in frozen if f is not None]
    if known and (len(known) != len(runs) or any(f != known[0] for f in known[1:])):
        raise ValueError("Frozen configuration mismatch; stability requires identical source/version/field/budget records")
    for key in ("model", "model_version", "knowledge_hash", "field_keys", "limits", "manifest_hash",
                "artifact_profile", "pymupdf_version", "render_dpi"):
        values = [r.get("run_metadata", {}).get(key) for r in runs]
        if values and any(value != values[0] for value in values[1:]):
            raise ValueError(f"Run metadata mismatch for {key}; stability requires fixed versions and scope")
    hashes = [{str(d["id"]): d.get("sha256") for d in run.get("documents", [])} for run in runs]
    if hashes and any(h != hashes[0] for h in hashes[1:]):
        raise ValueError("Document file hash/sample scope mismatch across stability runs")
    signatures = []
    for run in runs:
        signatures.append({
            (str(doc["id"]), field["field_key"]): frozenset(fact_key(f) for f in field.get("facts", []))
            for doc in run.get("documents", []) for field in doc.get("fields", [])
        })
    keys = set().union(*(set(s) for s in signatures)) if signatures else set()
    changed = [k for k in sorted(keys) if len({s.get(k) for s in signatures}) > 1]
    evidence_signatures = [{(str(doc["id"]), field["field_key"]): json.dumps(
        sorted([{"fact": str(fact_key(f)), "evidence": f.get("evidence", [])}
                for f in field.get("facts", [])], key=lambda x: x["fact"]), sort_keys=True)
        for doc in run.get("documents", []) for field in doc.get("fields", [])} for run in runs]
    changed_evidence = [k for k in sorted(keys) if len({s.get(k) for s in evidence_signatures}) > 1]
    return {"runs": len(runs), "field_tasks": len(keys), "changed_tasks": changed,
            "changed_evidence_tasks": changed_evidence,
            "configuration_status": "FROZEN" if known else "LEGACY_UNVERIFIED",
            "agreement_fraction": ratio(len(keys) - len(changed), len(keys)) if len(runs) >= 2 else None,
            "stable_fraction": ratio(len(keys) - len(changed), len(keys)) if len(runs) >= 2 else None,
            "controlled_stable_fraction": ratio(len(keys) - len(changed), len(keys)) if len(runs) >= 2 and known else None,
            "three_run_complete": len(runs) == 3,
            "warning": "Repeat agreement measures stability, not accuracy; do not mix model/knowledge/file versions.",
            "runtime_comparison": [{"token_usage": r.get("run_metadata", {}).get("token_usage"),
                                    "coverage": r.get("run_metadata", {}).get("coverage"),
                                    "status_counts": dict(Counter(d.get("status", "UNKNOWN") for d in r.get("documents", [])))} for r in runs],
            "run_metadata": [r.get("run_metadata", {}) for r in runs]}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    select = sub.add_parser("select")
    select.add_argument("--inventory", default=str(ROOT / "output/gas_report_audit/inventory.json"))
    select.add_argument("--count", type=int, default=26)
    select.add_argument("--seed", default="agent-v1")
    select.add_argument("--checklist-type", default="gas_report_agent")
    select.add_argument("--out", required=True)
    template = sub.add_parser("annotation-template")
    template.add_argument("--manifest", required=True)
    template.add_argument("--fields", nargs="+", default=DEFAULT_FIELDS)
    template.add_argument("--out", required=True)
    validate = sub.add_parser("validate-annotations")
    validate.add_argument("--gold", required=True)
    validate.add_argument("--sample-root", type=Path,
                          help="package root: verifies original PDFs/hash/pages and physical bbox bounds")
    evaluate_cmd = sub.add_parser("evaluate")
    evaluate_cmd.add_argument("--gold", required=True)
    evaluate_cmd.add_argument("--predictions", required=True)
    evaluate_cmd.add_argument("--evidence-iou", type=float)
    evaluate_cmd.add_argument("--out", required=True)
    evaluate_cmd.add_argument("--timings", help="JSON list of explicit active-work records")
    import_cmd = sub.add_parser("import-results")
    import_cmd.add_argument("--manifest", required=True)
    import_cmd.add_argument("--document-id", type=int, required=True)
    import_cmd.add_argument("--api-json", required=True)
    import_cmd.add_argument("--out", required=True)
    repeat = sub.add_parser("stability")
    repeat.add_argument("--predictions", nargs="+", required=True)
    repeat.add_argument("--out", required=True)
    args = parser.parse_args()
    if args.command == "select":
        result = select_samples(read_json(args.inventory), args.count, args.seed)
        result["checklist_type"] = args.checklist_type
    elif args.command == "annotation-template":
        result = annotation_template(read_json(args.manifest), args.fields)
    elif args.command == "validate-annotations":
        annotations = read_json(args.gold)
        errors = validate_annotations(annotations, args.sample_root)
        warnings = (["Physical page bounds unavailable; provide page_bounds or --sample-root with copied_file"]
                    if any(not d.get("page_bounds") and not (args.sample_root and d.get("copied_file"))
                           for d in annotations.get("documents", [])) else [])
        print(json.dumps({"valid": not errors, "errors": errors, "warnings": warnings}, ensure_ascii=False))
        raise SystemExit(1 if errors else 0)
    elif args.command == "evaluate":
        if args.evidence_iou is not None and not 0 <= args.evidence_iou <= 1:
            parser.error("--evidence-iou must be between 0 and 1")
        result = evaluate(read_json(args.gold), read_json(args.predictions), args.evidence_iou,
                          read_json(args.timings) if args.timings else None)
    elif args.command == "import-results":
        result = import_results(read_json(args.manifest), args.document_id, read_json(args.api_json))
    else:
        result = stability([read_json(p) for p in args.predictions])
    write_json(args.out, result)
    print(json.dumps(result.get("summary", {"output": args.out, "status": result.get("status")}), ensure_ascii=False))


if __name__ == "__main__":
    main()
