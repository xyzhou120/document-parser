"""基于快照的规则发布，使用串行化的乐观发布变更。"""

from copy import deepcopy
from dataclasses import asdict
from types import SimpleNamespace
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.database import ParseRun
from app.models.releases import ChecklistReleaseHead, RuleBundle, RuleReleaseEvent
from app.models.rules import ChecklistType, ExtractionRule
from app.rules.spec import RuleSpec
from app.schemas.releases import DraftBundlePayload, ReleaseCommand
from app.schemas.rules import RulePayload


def _hash(value):
    # 使用与 ParseRun.extraction_profile 完全相同的摘要。
    from app.services.parse_service import knowledge_hash
    return knowledge_hash(value)


def bundle_specs(bundle: RuleBundle) -> list[RuleSpec]:
    return [RuleSpec.from_orm(SimpleNamespace(**deepcopy(rule)))
            for rule in bundle.rules if rule["enabled"]]


def normalize_rules(checklist_type: str, rules: list[dict]) -> list[dict]:
    normalized = []
    seen = set()
    for rule in rules:
        if rule.get("checklist_type", checklist_type) != checklist_type:
            raise HTTPException(422, "规则不能跨清单发布")
        try:
            data = RulePayload.model_validate({**rule, "checklist_type": checklist_type}).model_dump()
            data["rule_id"] = str(UUID(str(rule["rule_id"]))) if rule.get("rule_id") else str(
                uuid5(NAMESPACE_URL, f"rule:{checklist_type}:{data['field_key']}"))
            data["version"] = int(rule.get("version", 1))
            if data["version"] < 1:
                raise ValueError("规则版本必须为正整数")
        except (ValidationError, ValueError, TypeError) as exc:
            raise HTTPException(422, f"规则配置无效: {exc}") from exc
        if data["field_key"] in seen:
            raise HTTPException(422, f"字段重复: {data['field_key']}")
        seen.add(data["field_key"])
        normalized.append(data)
    return sorted(normalized, key=lambda rule: (-rule["priority"], rule["field_key"]))


def validate_bundle(bundle: RuleBundle) -> dict:
    try:
        normalized = normalize_rules(bundle.checklist_type, bundle.rules)
        specs = [asdict(spec) for spec in bundle_specs(bundle)]
        errors = []
        if normalized != bundle.rules or _hash(normalized) != bundle.content_hash:
            errors.append("bundle 内容或 hash 不一致")
        if _hash(specs) != bundle.knowledge_hash:
            errors.append("运行知识 hash 不一致")
        if not specs:
            errors.append("至少需要一条启用规则")
    except (HTTPException, KeyError, AttributeError) as exc:
        errors = [str(exc)]
        specs = []
    return {"valid": not errors, "errors": errors, "enabled_rule_count": len(specs),
            "verification_status": "UNVERIFIED",
            "warnings": ["配置校验不证明提取效果；模型置信度未校准"]}


def diff_bundles(bundle: RuleBundle, baseline: RuleBundle | None) -> dict:
    old = {r["field_key"]: r for r in baseline.rules} if baseline else {}
    new = {r["field_key"]: r for r in bundle.rules}
    return {"baseline_bundle_id": str(baseline.bundle_id) if baseline else None,
            "bundle_id": str(bundle.bundle_id),
            "added": sorted(new.keys() - old.keys()), "removed": sorted(old.keys() - new.keys()),
            "changed": [{"field_key": key,
                         "changed_properties": sorted(k for k in old[key].keys() | new[key].keys()
                                                       if old[key].get(k) != new[key].get(k)),
                         "before": old[key], "after": new[key]}
                        for key in sorted(old.keys() & new.keys()) if old[key] != new[key]]}


class ReleaseService:
    async def _checklist(self, db: AsyncSession, checklist_type: str, *, lock=False):
        query = select(ChecklistType).where(ChecklistType.checklist_type == checklist_type)
        if lock:
            query = query.with_for_update().execution_options(populate_existing=True)
        item = (await db.execute(query)).scalar_one_or_none()
        if item is None:
            raise HTTPException(404, "清单不存在")
        if not item.is_active:
            raise HTTPException(409, "清单已禁用")
        return item

    async def head(self, db: AsyncSession, checklist_type: str):
        # 等待其他写入方的行锁后，避免使用身份映射中的过期状态。
        return (await db.execute(select(ChecklistReleaseHead).where(
            ChecklistReleaseHead.checklist_type == checklist_type
        ).execution_options(populate_existing=True))).scalar_one_or_none()

    async def get(self, db: AsyncSession, bundle_id: UUID) -> RuleBundle:
        bundle = await db.get(RuleBundle, bundle_id)
        if bundle is None:
            raise HTTPException(404, "规则 bundle 不存在")
        return bundle

    async def active_specs(self, db: AsyncSession, checklist_type: str):
        head = await self.head(db, checklist_type)
        if head is None:
            return None
        bundle = await self.get(db, head.bundle_id)
        if not validate_bundle(bundle)["valid"]:
            raise HTTPException(409, "已发布 bundle 损坏，不能回退至未发布编辑规则")
        return bundle_specs(bundle)

    async def create_draft(self, db: AsyncSession, payload: DraftBundlePayload):
        await self._checklist(db, payload.checklist_type, lock=True)
        head = await self.head(db, payload.checklist_type)
        source = payload.rules
        if source is None:
            from app.rules.repository import rule_repository
            rows = (await db.execute(select(ExtractionRule).where(
                ExtractionRule.checklist_type == payload.checklist_type))).scalars().all()
            source = [rule_repository.rule_to_dict(rule) for rule in rows]
        rules = normalize_rules(payload.checklist_type, source)
        last_version = await db.scalar(select(func.max(RuleBundle.version)).where(
            RuleBundle.checklist_type == payload.checklist_type))
        bundle = RuleBundle(bundle_id=uuid4(), checklist_type=payload.checklist_type,
                            version=(last_version or 0) + 1,
                            base_generation=head.generation if head else 0, rules=rules,
                            content_hash=_hash(rules), knowledge_hash="", note=payload.note,
                            operator=payload.operator)
        bundle.knowledge_hash = _hash([asdict(spec) for spec in bundle_specs(bundle)])
        db.add(bundle)
        db.add(RuleReleaseEvent(bundle_id=bundle.bundle_id, checklist_type=bundle.checklist_type,
                               action="DRAFT", generation=bundle.base_generation,
                               operator=payload.operator, reason=payload.note, sample_runs=[]))
        await db.flush()
        return bundle

    async def has_been_published(self, db: AsyncSession, bundle_id: UUID):
        return bool(await db.scalar(select(RuleReleaseEvent.event_id).where(
            RuleReleaseEvent.bundle_id == bundle_id,
            RuleReleaseEvent.action.in_(["PUBLISH", "ROLLBACK"])).limit(1)))

    async def describe(self, db: AsyncSession, bundle: RuleBundle):
        head = await self.head(db, bundle.checklist_type)
        return {"bundle_id": bundle.bundle_id, "checklist_type": bundle.checklist_type,
                "version": bundle.version, "base_generation": bundle.base_generation,
                "content_hash": bundle.content_hash, "knowledge_hash": bundle.knowledge_hash,
                "rules": bundle.rules, "note": bundle.note, "operator": bundle.operator,
                "created_at": bundle.created_at,
                "is_active": bool(head and head.bundle_id == bundle.bundle_id),
                "has_been_published": await self.has_been_published(db, bundle.bundle_id),
                "current_generation": head.generation if head else 0}

    async def preview(self, db: AsyncSession, bundle: RuleBundle, baseline_id: UUID | None = None):
        await self._checklist(db, bundle.checklist_type)
        head = await self.head(db, bundle.checklist_type)
        baseline = await self.get(db, baseline_id or head.bundle_id) if baseline_id or head else None
        if baseline and baseline.checklist_type != bundle.checklist_type:
            raise HTTPException(422, "不能跨清单对比")
        return {"mode": "CONFIGURATION_DRY_RUN", "model_called": False,
                "validation": validate_bundle(bundle), "diff": diff_bundles(bundle, baseline),
                "knowledge_hash": bundle.knowledge_hash,
                "runtime_rules": [asdict(spec) for spec in bundle_specs(bundle)],
                "fields": [{"field_key": spec.field_key, "methods": spec.methods,
                            "identity_dimensions": list(spec.dimension_schema),
                            "multi_instance": spec.multi_instance} for spec in bundle_specs(bundle)]}

    async def _sample_runs(self, db: AsyncSession, bundle: RuleBundle, run_ids: list[UUID]):
        result = []
        for run_id in dict.fromkeys(run_ids):
            run = await db.get(ParseRun, run_id)
            if run is None:
                raise HTTPException(422, f"试跑记录不存在: {run_id}")
            if (run.checklist_type != bundle.checklist_type or
                    (run.extraction_profile or {}).get("knowledge_hash") != bundle.knowledge_hash):
                raise HTTPException(422, "试跑清单或知识 hash 与 bundle 不匹配")
            if run.status != "COMPLETED":
                raise HTTPException(422, "只能绑定已完成的试跑")
            result.append({"parse_run_id": str(run_id), "document_id": str(run.document_id),
                           "status": run.status, "knowledge_hash": bundle.knowledge_hash,
                           "verification_status": "UNVERIFIED"})
        return result

    async def activate(self, db: AsyncSession, bundle_id: UUID, command: ReleaseCommand,
                       *, rollback=False):
        bundle = await self.get(db, bundle_id)
        await self._checklist(db, bundle.checklist_type, lock=True)
        head = await self.head(db, bundle.checklist_type)
        generation = head.generation if head else 0
        if generation != command.expected_generation:
            raise HTTPException(409, "发布基版本已变化，请重新预览")
        if not rollback and bundle.base_generation != generation:
            raise HTTPException(409, "草稿基版本已变化，请基于当前发布创建新草稿")
        if rollback and not await self.has_been_published(db, bundle_id):
            raise HTTPException(409, "只能回滚到曾发布的 bundle")
        if head and head.bundle_id == bundle_id:
            raise HTTPException(409, "bundle 已是当前发布版本")
        validation = validate_bundle(bundle)
        if not validation["valid"]:
            raise HTTPException(422, validation)
        samples = await self._sample_runs(db, bundle, command.sample_run_ids)
        previous = head.bundle_id if head else None
        event = RuleReleaseEvent(bundle_id=bundle_id, checklist_type=bundle.checklist_type,
                                 previous_bundle_id=previous, action="ROLLBACK" if rollback else "PUBLISH",
                                 generation=generation + 1, operator=command.operator,
                                 reason=command.reason, sample_runs=samples,
                                 sample_report={"kind": "USER_SUPPLIED", "verified_by_platform": False,
                                                "report": command.sample_report}
                                 if command.sample_report is not None else None)
        if head:
            head.bundle_id = bundle_id
            head.generation = generation + 1
        else:
            db.add(ChecklistReleaseHead(checklist_type=bundle.checklist_type,
                                        bundle_id=bundle_id, generation=generation + 1))
        db.add(event)
        await db.flush()
        return event


release_service = ReleaseService()
