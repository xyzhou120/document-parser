"""隔离的 P20 可靠提取评测入口。

运行器只读取 JSON 文件。它不会打开应用数据库、启动工作进程、调用模型或修改运行记录。
Gold 是预期维度和证据的唯一来源；运行导出结果只适配为 scripts.evaluation_v2
消费的预测结构。
"""

from __future__ import annotations

import argparse
import json
from copy import deepcopy
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from scripts.evaluation_v2 import evaluate


def _read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"JSON root must be an object: {path}")
    return value


def adapt_run_export(export: dict[str, Any]) -> dict[str, Any]:
    """转换 v2 事实或执行导出结果，但不伪造证据。

    复制已有的 documents[].rules 预测。兼容导出结果可以使用 fields；只映射名称和已提供
    的事实字段。缺失的维度、证据、状态和范围闭合信息继续保持缺失，防止评分器把不完整
    导出结果判为通过。
    """

    if isinstance(export.get("documents"), list) and all(
        isinstance(document, dict) and isinstance(document.get("rules"), list)
        for document in export["documents"]
    ):
        result = deepcopy(export)
        for document in result["documents"]:
            for rule in document["rules"]:
                if isinstance(rule, dict) and isinstance(rule.get("facts"), list):
                    rule["facts"] = [_adapt_fact(fact) for fact in rule["facts"]
                                     if isinstance(fact, dict)]
        return result
    sources = export.get("documents")
    if sources is None:
        sources = [export]
    if not isinstance(sources, list):
        raise ValueError("run export documents must be a list")
    documents: list[dict[str, Any]] = []
    for document in sources:
        if not isinstance(document, dict):
            continue
        fact_view = document.get("fact_view") or document
        execution = document.get("execution") or {}
        doc_id = document.get("document_id", fact_view.get("document_id", document.get("id")))
        if doc_id is None:
            raise ValueError("run export must supply document_id; no gold-based mapping is inferred")
        jobs = {str(job.get("field_key")): job for job in execution.get("jobs", [])
                if isinstance(job, dict)}
        fields = document.get("fields")
        if fields is None:
            grouped: dict[str, dict[str, Any]] = {}
            for fact in fact_view.get("facts", []):
                if isinstance(fact, dict) and fact.get("field_key"):
                    key = str(fact["field_key"])
                    grouped.setdefault(key, {"field_key": key, "facts": []})["facts"].append(_adapt_fact(fact))
            fields = list(grouped.values())
            for key in jobs:
                if key not in grouped:
                    fields.append({"field_key": key, "facts": []})
        if isinstance(fields, dict):
            fields = [dict(value, field_key=key) for key, value in fields.items()
                      if isinstance(value, dict)]
        rules = []
        for field in fields if isinstance(fields, list) else []:
            if not isinstance(field, dict):
                continue
            rule = dict(field)
            rule["field_key"] = field.get("field_key", field.get("rule_id"))
            job = jobs.get(str(rule["field_key"]), {})
            checkpoint = job.get("checkpoint") or {}
            completion = checkpoint.get("field_completion") or checkpoint.get("completion") or {}
            for key, value in {
                "semantic_state": job.get("semantic_state"),
                "job_created": bool(job),
                "attempted": job.get("attempt", 0) > 0,
                "rule_semantic_hash": job.get("rule_hash"),
                "range_closed": completion.get("range_closed", completion.get("coverage_complete")),
                "proof_valid": completion.get("proof_valid"),
            }.items():
                if key not in rule and value is not None:
                    rule[key] = value
            if job:
                rule["execution_provenance"] = deepcopy(job)
            # 解析导出结果有时会把事实放在 values 下。
            # 不做转换或证据回退，直接保留已提供的事实。
            if "facts" not in rule and isinstance(field.get("values"), list):
                rule["facts"] = field["values"]
            if isinstance(rule.get("facts"), list):
                rule["facts"] = [_adapt_fact(fact) for fact in rule["facts"]
                                 if isinstance(fact, dict)]
            rules.append(rule)
        row = {"document_id": doc_id, "rules": rules, "run_export_provenance": deepcopy(document)}
        for key in ("file_hash", "sha256", "run_id", "content_revision_id", "model",
                    "model_version", "contract_version", "usage", "attempt", "provenance"):
            if key in document:
                row[key] = document[key]
        if "sha256" not in row and document.get("file_hash"):
            row["sha256"] = document["file_hash"]
        elif "sha256" not in row and export.get("file_hash"):
            row["sha256"] = export["file_hash"]
        documents.append(row)
    result = {"documents": documents}
    for key in ("run_id", "file_hash", "content_revision_id", "model", "model_version",
                "contract_version", "usage", "attempt", "provenance"):
        if key in export:
            result[key] = export[key]
    return result


def _adapt_fact(fact: dict[str, Any]) -> dict[str, Any]:
    result = deepcopy(fact)
    assertions = fact.get("assertions") or {}
    if "attribute_evidence" not in result:
        attributes: dict[str, Any] = {}
        for key, assertion in assertions.items():
            if not isinstance(assertion, dict) or assertion.get("status") != "supported":
                continue
            ids = deepcopy(assertion.get("evidence_ids") or [])
            if key.startswith("dimensions."):
                attributes.setdefault("dimensions", {})[key.split(".", 1)[1]] = ids
            else:
                attributes[key] = ids
        result["attribute_evidence"] = attributes
    if "table_relation" not in result and isinstance(assertions.get("table_relation"), dict):
        if "value" in assertions["table_relation"]:
            result["table_relation"] = deepcopy(assertions["table_relation"]["value"])
    if "occurrences" not in result:
        result["occurrences"] = [dict(item, occurrence_id=item.get("occurrence_id", item.get("evidence_id")))
                                 for item in fact.get("evidence", []) if isinstance(item, dict)]
    if "available_for_downstream" in fact:
        result.setdefault("is_determined", fact.get("available_for_downstream") is True)
    return result


def run_evaluation(*, gold_path: Path, predictions_path: Path, output_path: Path,
                   from_run_export: bool = False, require_adjudicated: bool = False,
                   evidence_iou: float = 1.0) -> dict[str, Any]:
    if output_path.resolve() in {gold_path.resolve(), predictions_path.resolve()}:
        raise ValueError("output path must not overwrite a gold or prediction input")
    gold = _read_json(gold_path)
    raw_predictions = _read_json(predictions_path)
    predictions = adapt_run_export(raw_predictions) if from_run_export else raw_predictions
    result = evaluate(gold, predictions, evidence_iou=evidence_iou,
                      require_adjudicated=require_adjudicated)
    summary = result["summary"]
    threshold_status = gold.get("quality_thresholds", {}).get("status")
    thresholds = gold.get("quality_thresholds", {}).get("metrics") or {}
    publish_evaluation = bool(
        result["status"] == "EVALUATED"
        and summary.get("pending_adjudication_tasks", 0) == 0
        and threshold_status == "APPROVED"
        and bool(thresholds)
    )
    report = {
        "report_schema_version": "reliable-evaluation-report-v1",
        "generated_at": datetime.now(UTC).replace(microsecond=0).isoformat(),
        "inputs": {"gold": str(gold_path), "predictions": str(predictions_path),
                   "from_run_export": from_run_export},
        "provenance": {
            "run_id": raw_predictions.get("run_id"),
            "file_hash": raw_predictions.get("file_hash"),
            "content_revision_id": raw_predictions.get("content_revision_id"),
            "model": raw_predictions.get("model"),
            "model_version": raw_predictions.get("model_version"),
            "usage": raw_predictions.get("usage"),
            "attempt": raw_predictions.get("attempt"),
        },
        "evaluation": result,
        "release_assessment": {
            "status": "NOT_ASSESSED",
            "readiness": "READY_FOR_QUALITY_REVIEW" if publish_evaluation else "BLOCKED",
            "reasons": ["release_gates_and_statistical_thresholds_not_executed", *([] if publish_evaluation else [
                reason for reason in (
                    "no_adjudicated_gold" if result["status"] != "EVALUATED" else None,
                    "pending_gold_adjudication" if summary.get("pending_adjudication_tasks", 0) else None,
                    "gold_thresholds_pending_business_approval" if threshold_status != "APPROVED" else None,
                    "gold_threshold_metrics_missing" if not thresholds else None,
                ) if reason
            ])],
            "quality_thresholds_status": threshold_status or "MISSING",
        },
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--gold", required=True, type=Path)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--predictions", type=Path)
    source.add_argument("--from-run-export", type=Path)
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--require-adjudicated", action="store_true")
    parser.add_argument("--evidence-iou", type=float, default=1.0)
    args = parser.parse_args()
    report = run_evaluation(
        gold_path=args.gold,
        predictions_path=args.predictions or args.from_run_export,
        output_path=args.out,
        from_run_export=args.from_run_export is not None,
        require_adjudicated=args.require_adjudicated,
        evidence_iou=args.evidence_iou,
    )
    print(json.dumps(report["release_assessment"], ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
