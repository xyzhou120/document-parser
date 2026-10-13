"""Read-only planning and related fact candidates; no implicit source arbitration."""

import json
from collections import defaultdict
from dataclasses import asdict, is_dataclass

DISPLAY_KEYS = {"field_name_cn", "field_name", "priority", "version", "rule_id", "enabled"}
VALIDATION_KEYS = {"validation", "required"}


def plain_rule(rule):
    if isinstance(rule, dict):
        return rule
    if is_dataclass(rule):
        return asdict(rule)
    return {key: value for key, value in vars(rule).items() if not key.startswith("_")}


def plan_rule_impact(
    previous_profile, current_profile, *, artifact_compatible=True, full_reasons=None
):
    """Classification is advisory. Every changed active field is conservatively rerun."""
    old = {rule["field_key"]: plain_rule(rule) for rule in previous_profile.get("rules", [])}
    new = {rule["field_key"]: plain_rule(rule) for rule in current_profile.get("rules", [])}
    added, removed = sorted(new.keys() - old.keys()), sorted(old.keys() - new.keys())
    changed = []
    for key in sorted(new.keys() & old.keys()):
        keys = sorted(
            name
            for name in new[key].keys() | old[key].keys()
            if new[key].get(name) != old[key].get(name)
        )
        if not keys:
            continue
        category = (
            "semantic"
            if set(keys) - DISPLAY_KEYS - VALIDATION_KEYS
            else "validation" if set(keys) & VALIDATION_KEYS else "display"
        )
        changed.append(
            {
                "field_key": key,
                "category": category,
                "changed_properties": keys,
                "execution": "RERUN_FIELD",
            }
        )
    config_changed = [
        key
        for key in ("extraction_engine", "agent_config", "agent_model")
        if previous_profile.get(key) != current_profile.get(key)
    ]
    rerun = (
        sorted(new)
        if config_changed or not artifact_compatible
        else sorted(set(added) | {row["field_key"] for row in changed})
    )
    reasons = list(full_reasons or [])
    if not artifact_compatible and not reasons:
        reasons.append("ARTIFACT_PROFILE_MISMATCH")
    return {
        "suggested_mode": "INCREMENTAL" if artifact_compatible else "FULL",
        "added_fields": added,
        "removed_fields": removed,
        "changed_fields": changed,
        "rerun_fields": rerun,
        "inherited_fields": sorted(set(new) - set(rerun)),
        "configuration_changes": config_changed,
        "full_reasons": reasons,
        "has_changes": bool(added or removed or changed or config_changed),
        "policy": "conservative_field_rerun",
        "preview_only": True,
        "note": "显示与校验变更仅做分类提示，执行仍保守重跑受影响字段；未触发提取。",
    }


def related_fact_candidates(sources):
    """Group explicit same identities across different documents, retaining every fact.

    Unknown identities are deliberately ungrouped; identical numbers alone do
    not establish that two PDFs describe the same business object.
    """
    groups = defaultdict(list)
    standalone = []
    for source in sources:
        for fact in source.get("facts", []):
            row = {
                "document_id": source["document_id"],
                "file_name": source.get("file_name"),
                "parse_run_id": source.get("parse_run_id"),
                "relation_source_role": source.get("source_role"),
                "fact": fact,
            }
            dimensions = fact.get("dimensions") or {}
            role, kind = fact.get("source_role"), fact.get("value_kind")

            def known(value):
                return value is not None and str(value).strip().lower() not in {
                    "",
                    "unknown",
                    "未知",
                }

            if (
                not dimensions
                or not all(known(value) for value in dimensions.values())
                or not known(role)
                or not known(kind)
            ):
                standalone.append(row)
                continue
            identity = {
                "checklist_type": source.get("checklist_type"),
                "field_key": fact.get("field_key"),
                "dimensions": dimensions,
                "value_kind": kind,
                "source_role": role,
            }
            groups[json.dumps(identity, ensure_ascii=False, sort_keys=True, default=str)].append(
                row
            )
    candidates = []
    for identity, rows in groups.items():
        if len({str(row["document_id"]) for row in rows}) < 2:
            standalone.extend(rows)
            continue
        candidates.append(
            {
                "identity": json.loads(identity),
                "candidates": rows,
                "different_values": len(
                    {(row["fact"].get("normalized"), row["fact"].get("unit")) for row in rows}
                )
                > 1,
                "resolution": "UNDECIDED",
            }
        )
    return {
        "same_identity_candidates": candidates,
        "unmatched_facts": standalone,
        "policy": "source_listing_only",
        "note": "关系与人工上下文不改写事实身份；候选仅供比较，不裁决正确值。",
    }
