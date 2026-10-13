"""Repair only the named local runs; keep old checkpoints and call history.

The caller must explicitly name the configured database. This preserves frozen
old runs by superseding them and creating a repair child on the same revision.
"""

from __future__ import annotations

import argparse
import asyncio
import json
from copy import deepcopy
from pathlib import Path
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.config import settings
from app.models.database import ParseRun
from app.models.rules import ExtractionRule, RuleChangeLog
from app.rules.repository import rule_repository
from app.schemas.rules import RulePayload
from app.services.execution import JobRepository
from app.services.reliable_extraction_service import ReliableExtractionService


def compatible_rule(rule: ExtractionRule) -> dict:
    payload = rule_repository.rule_to_dict(rule)
    for key in ("rule_id", "version", "created_at", "updated_at"):
        payload.pop(key, None)
    payload = deepcopy(payload)
    key = payload["field_key"]
    source = payload.get("validation") or {}
    if source.get("source_definition") != "pdf_extract.fields.yaml":
        return RulePayload.model_validate(payload).model_dump()
    kind = source.get("value_type")
    # Keep field names and business meaning. Only adapt the value representation
    # to the platform's literal-value and dimension-evidence contract.
    if kind == "point_value":
        payload["data_type"] = "float"
        identity = ["workface", "measurement_point"]
        extra = "value只写测值数字。测点原文完整名称写dimensions.measurement_point；如相同编号在不同巷道出现，保留含巷道的原文完整测点名称，不自行拼造。不要把point/number对象写进value。"
    elif kind in {"period", "borehole_params"}:
        payload["data_type"] = "string"
        identity = ["workface"]
        extra = "value只摘录包含全部已知起止日期或钻孔参数的连续原文，不改写、不计算，不输出JSON对象。不同施工段或参数组分别一条，缺项不补。"
    else:
        identity = ["workface"]
        extra = "value仅写原文中的标量数值或文字，不输出JSON对象；不同评价单元或钻孔组按原文名称分别绑定，不合并相互独立的对象。"
    # One per evaluation unit is multiple identities across a whole attachment.
    payload["multi_instance"] = True
    contract = payload["strategy"].setdefault("semantic_contract", {"contract_version": 2})
    contract["cardinality"] = "all_occurrences" if kind in {"period", "borehole_params"} else "multi_identity"
    contract["identity_dimensions"] = identity
    contract["applicable_dimensions"] = identity
    agent = payload["strategy"]["agent_extract"]
    agent["identity_dimensions"] = identity
    base = str(payload.get("field_description") or "")
    agent["instructions"] = base + "\n平台取值格式：" + extra
    if key == "design_hole_count":
        payload["value_kind"] = "design"
    elif key == "desorbable_gas_limit":
        payload["value_kind"] = "threshold"
    return RulePayload.model_validate(payload).model_dump()


async def repair(database: str, run_ids: list[UUID], destination: Path, apply: bool):
    if database != settings.database.name:
        raise ValueError("Explicit database does not match current local configuration")
    engine = create_async_engine(settings.database.url)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    report: dict[str, Any] = {"database": database, "apply": apply, "runs": [], "rules_changed": []}
    try:
        async with sessions() as db, db.begin():
            originals = []
            for run_id in run_ids:
                run = await db.get(ParseRun, run_id, with_for_update=True)
                if run is None or run.contract_version != "reliable-extraction-v2":
                    raise ValueError("Repair target must be an existing reliable run")
                if run.status in {"CANCELLED", "COMPLETED", "COMPLETED_WITH_REVIEW"}:
                    report["runs"].append({"run_id": str(run_id), "status": run.status, "skipped": True})
                else:
                    originals.append(run)
            checklists = {run.checklist_type for run in originals}
            for checklist in checklists:
                rows = (await db.scalars(select(ExtractionRule).where(
                    ExtractionRule.checklist_type == checklist, ExtractionRule.enabled.is_(True)))).all()
                for row in rows:
                    before = rule_repository._snapshot(row)
                    payload = compatible_rule(row)
                    changed = any(getattr(row, key) != value for key, value in payload.items())
                    if not changed:
                        continue
                    report["rules_changed"].append({"field_key": row.field_key, "data_type": payload["data_type"],
                        "cardinality": payload["strategy"]["semantic_contract"]["cardinality"]})
                    if apply:
                        for key, value in payload.items():
                            setattr(row, key, value)
                        row.version += 1
                        db.add(RuleChangeLog(rule_id=row.rule_id, checklist_type=checklist,
                            action="repair_value_mapping", before_state=before,
                            after_state=rule_repository._snapshot(row), operator="incident-20261013"))
            await db.flush()
            service = ReliableExtractionService()
            for old in originals:
                if not apply:
                    report["runs"].append({"old_run_id": str(old.parse_run_id), "action": "create repair child"})
                    continue
                rules = await rule_repository.get_rules_for_checklist(db, old.checklist_type)
                child = await service.start(db, old.document_id, rules=rules,
                    revision_id=old.content_revision_id, parent_run_id=old.parse_run_id,
                    request_id=f"repair-20261013:{old.parse_run_id}")
                # Explicitly record the recovery policy in the new run before it
                # is dispatched. The original frozen model snapshot is untouched.
                profile = deepcopy(child.extraction_profile or {})
                profile["model_config"]["thinking_enabled"] = False
                profile["repair_reason"] = "duplicate wakes, invalid JSON and incompatible field representation"
                child.extraction_profile = profile
                await JobRepository(db).control_run(old.parse_run_id, "supersede")
                old.extraction_profile = {**(old.extraction_profile or {}), "successor_run_id": str(child.parse_run_id),
                    "repair_reason": profile["repair_reason"]}
                report["runs"].append({"old_run_id": str(old.parse_run_id), "new_run_id": str(child.parse_run_id),
                    "document_id": str(child.document_id), "revision_id": str(child.content_revision_id)})
            if not apply:
                await db.rollback()
    finally:
        await engine.dispose()
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--database", required=True)
    parser.add_argument("--run", type=UUID, action="append", required=True)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--apply", action="store_true")
    arguments = parser.parse_args()
    asyncio.run(repair(arguments.database, arguments.run, arguments.report, arguments.apply))
