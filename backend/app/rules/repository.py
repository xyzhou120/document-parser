"""规则仓储：清单类型与提取规则的读写、缓存、版本管理"""

import logging
import time
import uuid
from typing import Any

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models.rules import ChecklistType, ExtractionRule, RuleChangeLog
from app.rules.spec import RuleSpec

logger = logging.getLogger(__name__)

# 规则缓存有效期（秒）。规则变更时主动失效，无需等待过期。
_CACHE_TTL = 60


class RuleRepository:
    """规则仓储"""

    def __init__(self):
        self._spec_cache: dict[str, tuple[float, list[RuleSpec]]] = {}

    # ---- 清单类型 ----

    async def list_checklist_types(self, db: AsyncSession, only_active: bool = False) -> list[dict]:
        """清单类型列表，附带规则数量"""
        query = select(ChecklistType).order_by(
            ChecklistType.sort_order, ChecklistType.checklist_type
        )
        if only_active:
            query = query.where(ChecklistType.is_active.is_(True))
        result = await db.execute(query)
        types = result.scalars().all()

        counts = await self._rule_counts(db)
        return [
            {
                "checklist_type": item.checklist_type,
                "checklist_name": item.checklist_name,
                "description": item.description,
                "is_active": item.is_active,
                "sort_order": item.sort_order,
                "rule_count": counts.get(item.checklist_type, 0),
                "created_at": item.created_at,
                "updated_at": item.updated_at,
            }
            for item in types
        ]

    async def _rule_counts(self, db: AsyncSession) -> dict[str, int]:
        result = await db.execute(
            select(ExtractionRule.checklist_type, func.count(ExtractionRule.rule_id)).group_by(
                ExtractionRule.checklist_type
            )
        )
        return {row[0]: row[1] for row in result.all()}

    async def get_checklist_type(
        self, db: AsyncSession, checklist_type: str
    ) -> ChecklistType | None:
        return await db.get(ChecklistType, checklist_type)

    async def upsert_checklist_type(
        self, db: AsyncSession, payload: dict, operator: str = "system"
    ) -> ChecklistType:
        """新建或更新清单类型"""
        checklist_type = payload["checklist_type"]
        entity = await db.get(ChecklistType, checklist_type)
        before = self._snapshot(entity) if entity else None

        if entity is None:
            entity = ChecklistType(
                checklist_type=checklist_type,
                checklist_name=payload.get("checklist_name") or checklist_type,
                description=payload.get("description"),
                is_active=payload.get("is_active", True),
                sort_order=payload.get("sort_order", 100),
            )
            db.add(entity)
            action = "create"
        else:
            for field in ("checklist_name", "description", "is_active", "sort_order"):
                if field in payload:
                    setattr(entity, field, payload[field])
            action = "update"

        await db.flush()
        await self._log_change(
            db,
            rule_id=None,
            checklist_type=checklist_type,
            action=f"checklist_type_{action}",
            before=before,
            after=self._snapshot(entity),
            operator=operator,
        )
        await db.commit()
        self.invalidate_cache(checklist_type)
        return entity

    async def delete_checklist_type(self, db: AsyncSession, checklist_type: str) -> bool:
        entity = await db.get(ChecklistType, checklist_type)
        if entity is None:
            return False
        before = self._snapshot(entity)
        await db.delete(entity)
        await db.execute(
            delete(ExtractionRule).where(ExtractionRule.checklist_type == checklist_type)
        )
        await self._log_change(
            db,
            rule_id=None,
            checklist_type=checklist_type,
            action="checklist_type_delete",
            before=before,
            after=None,
            operator="system",
        )
        await db.commit()
        self.invalidate_cache(checklist_type)
        return True

    # ---- 提取规则 ----

    async def list_rules(
        self,
        db: AsyncSession,
        checklist_type: str | None = None,
        only_enabled: bool = False,
    ) -> list[ExtractionRule]:
        query = select(ExtractionRule).order_by(
            ExtractionRule.priority.desc(), ExtractionRule.field_key
        )
        if checklist_type:
            query = query.where(ExtractionRule.checklist_type == checklist_type)
        if only_enabled:
            query = query.where(ExtractionRule.enabled.is_(True))
        result = await db.execute(query)
        return list(result.scalars().all())

    async def get_rule(self, db: AsyncSession, rule_id: uuid.UUID) -> ExtractionRule | None:
        return await db.get(ExtractionRule, rule_id)

    async def create_rule(
        self, db: AsyncSession, payload: dict, operator: str = "system"
    ) -> ExtractionRule:
        """新建规则"""
        rule = ExtractionRule(
            checklist_type=payload["checklist_type"],
            field_key=payload["field_key"],
            field_name_cn=payload.get("field_name_cn") or payload["field_key"],
            target_type=payload.get("target_type") or "fact",
            target_group_key=payload.get("target_group_key"),
            target_group_name=payload.get("target_group_name"),
            output_schema=payload.get("output_schema") or {},
            source_policy=payload.get("source_policy") or {},
            evidence_policy=payload.get("evidence_policy") or {},
            field_description=payload.get("field_description"),
            dimension_schema=payload.get("dimension_schema") or {},
            value_kind=payload.get("value_kind") or "unknown",
            data_type=payload.get("data_type") or "string",
            unit=payload.get("unit"),
            required=bool(payload.get("required")),
            multi_instance=bool(payload.get("multi_instance")),
            merge_strategy=payload.get("merge_strategy") or "value_consistent",
            strategy=payload.get("strategy") or {},
            validation=payload.get("validation"),
            fallback_value=payload.get("fallback_value"),
            priority=int(payload.get("priority") or 50),
            enabled=payload.get("enabled", True),
            version=1,
        )
        db.add(rule)
        await db.flush()
        await self._log_change(
            db,
            rule_id=rule.rule_id,
            checklist_type=rule.checklist_type,
            action="create",
            before=None,
            after=self._snapshot(rule),
            operator=operator,
        )
        await db.commit()
        self.invalidate_cache(rule.checklist_type)
        return rule

    async def update_rule(
        self, db: AsyncSession, rule_id: uuid.UUID, payload: dict, operator: str = "system"
    ) -> ExtractionRule | None:
        """修改规则，版本号自增"""
        rule = await db.get(ExtractionRule, rule_id)
        if rule is None:
            return None

        before = self._snapshot(rule)
        editable = (
            "field_key",
            "field_name_cn",
            "target_type",
            "target_group_key",
            "target_group_name",
            "output_schema",
            "source_policy",
            "evidence_policy",
            "field_description",
            "dimension_schema",
            "value_kind",
            "data_type",
            "unit",
            "required",
            "multi_instance",
            "merge_strategy",
            "strategy",
            "validation",
            "fallback_value",
            "priority",
            "enabled",
        )
        for field in editable:
            if field in payload:
                setattr(rule, field, payload[field])
        rule.version = (rule.version or 1) + 1

        await db.flush()
        await self._log_change(
            db,
            rule_id=rule.rule_id,
            checklist_type=rule.checklist_type,
            action="update",
            before=before,
            after=self._snapshot(rule),
            operator=operator,
        )
        await db.commit()
        self.invalidate_cache(rule.checklist_type)
        return rule

    async def toggle_rule(
        self, db: AsyncSession, rule_id: uuid.UUID, enabled: bool, operator: str = "system"
    ) -> ExtractionRule | None:
        rule = await db.get(ExtractionRule, rule_id)
        if rule is None:
            return None
        before = self._snapshot(rule)
        rule.enabled = enabled
        await db.flush()
        await self._log_change(
            db,
            rule_id=rule.rule_id,
            checklist_type=rule.checklist_type,
            action="toggle",
            before=before,
            after=self._snapshot(rule),
            operator=operator,
        )
        await db.commit()
        self.invalidate_cache(rule.checklist_type)
        return rule

    async def delete_rule(
        self, db: AsyncSession, rule_id: uuid.UUID, operator: str = "system"
    ) -> bool:
        rule = await db.get(ExtractionRule, rule_id)
        if rule is None:
            return False
        before = self._snapshot(rule)
        checklist_type = rule.checklist_type
        await db.delete(rule)
        await self._log_change(
            db,
            rule_id=rule_id,
            checklist_type=checklist_type,
            action="delete",
            before=before,
            after=None,
            operator=operator,
        )
        await db.commit()
        self.invalidate_cache(checklist_type)
        return True

    async def bulk_import(
        self,
        db: AsyncSession,
        checklist_type: str,
        rules: list[dict],
        operator: str = "system",
    ) -> dict:
        """批量导入规则：同 field_key 覆盖更新，不存在则新建"""
        created, updated, failed = 0, 0, []
        if len(rules) > 1000:
            raise ValueError("单次最多导入 1000 条规则")

        for payload in rules:
            if not isinstance(payload, dict):
                failed.append({"reason": "规则必须为对象"})
                continue
            field_key = str(payload.get("field_key") or "").strip()
            if not field_key:
                failed.append({"reason": "缺少 field_key", "payload": payload})
                continue

            try:
                # 独立校验每条记录，使一条格式错误记录被报告时其余导入仍可继续。
                from app.schemas.rules import RulePayload

                validated = RulePayload(**{**payload, "checklist_type": checklist_type})
                existing = await db.execute(
                    select(ExtractionRule).where(
                        ExtractionRule.checklist_type == checklist_type,
                        ExtractionRule.field_key == field_key,
                    )
                )
                entity = existing.scalar_one_or_none()
                if entity is None:
                    await self.create_rule(db, validated.model_dump(), operator)
                    created += 1
                else:
                    await self.update_rule(
                        db, entity.rule_id, {**payload, "checklist_type": checklist_type}, operator
                    )
                    updated += 1
            except Exception as exc:
                logger.error("导入规则 %s 失败: %s", field_key, exc)
                await db.rollback()
                failed.append({"field_key": field_key, "reason": str(exc)})

        self.invalidate_cache(checklist_type)
        return {"created": created, "updated": updated, "failed": failed}

    async def list_change_log(
        self, db: AsyncSession, checklist_type: str | None = None, limit: int = 100
    ) -> list[RuleChangeLog]:
        query = select(RuleChangeLog).order_by(RuleChangeLog.created_at.desc()).limit(limit)
        if checklist_type:
            query = query.where(RuleChangeLog.checklist_type == checklist_type)
        result = await db.execute(query)
        return list(result.scalars().all())

    # ---- 供解析链路使用 ----

    async def get_rules_for_checklist(
        self, db: AsyncSession, checklist_type: str | None
    ) -> list[RuleSpec]:
        """取所选清单的启用规则；明确选择时不跨清单回退。"""
        if checklist_type:
            # Celery 工作进程运行期间，其他 API 进程可能修改规则。
            # 此处不能使用进程本地规则定义：每次解析都必须读取当前数据库状态。
            checklist = await self.get_checklist_type(db, checklist_type)
            if checklist is None or not checklist.is_active:
                return []

            # CRUD is the active source for new runs. Release bundles remain
            # immutable audit/rollback artifacts and can still be selected by
            # an explicit run request.
            rules = await self.list_rules(db, checklist_type, only_enabled=True)
            if rules:
                specs = [RuleSpec.from_orm(rule) for rule in rules]
                return specs

            # Compatibility for legacy checklists that only have an immutable
            # release bundle. New and edited rules always take precedence.
            from app.models.releases import ChecklistReleaseHead, RuleBundle
            head = await db.get(ChecklistReleaseHead, checklist_type)
            if head is not None and getattr(head, "bundle_id", None):
                bundle = await db.get(RuleBundle, head.bundle_id)
                from app.services.release_service import bundle_specs, validate_bundle
                if bundle is None or not validate_bundle(bundle)["valid"]:
                    raise ValueError("已发布规则 bundle 校验失败")
                return bundle_specs(bundle)

            logger.warning("清单 %s 没有启用的规则", checklist_type)

        # 明确指定清单时不允许跨清单借用规则，避免提取出错误字段。
        if checklist_type:
            return []

        # 未指定清单时才使用配置文件兜底字段。
        return self._config_fallback()

    def _config_fallback(self) -> list[RuleSpec]:
        """配置文件兜底，保证任何情况下解析链路都有规则可用"""
        fields = (settings.fields or {}).get("default") or []
        specs = [RuleSpec.from_config(field) for field in fields]
        if specs:
            logger.info("使用配置文件兜底规则 %d 条", len(specs))
        return specs

    async def get_rules_for_document(self, db: AsyncSession, document) -> list[RuleSpec]:
        """按文档的清单类型取规则"""
        checklist_type = getattr(document, "checklist_type", None)
        return await self.get_rules_for_checklist(db, checklist_type)

    # ---- 缓存 ----

    def invalidate_cache(self, checklist_type: str | None = None) -> None:
        if checklist_type is None:
            self._spec_cache.clear()
            return
        self._spec_cache.pop(checklist_type, None)

    def _from_cache(self, checklist_type: str) -> list[RuleSpec] | None:
        entry = self._spec_cache.get(checklist_type)
        if not entry:
            return None
        created_at, specs = entry
        if time.time() - created_at > _CACHE_TTL:
            self._spec_cache.pop(checklist_type, None)
            return None
        return specs

    def _to_cache(self, checklist_type: str, specs: list[RuleSpec]) -> None:
        self._spec_cache[checklist_type] = (time.time(), specs)

    # ---- 工具 ----

    async def _log_change(
        self,
        db: AsyncSession,
        rule_id: uuid.UUID | None,
        checklist_type: str | None,
        action: str,
        before: dict | None,
        after: dict | None,
        operator: str,
    ) -> None:
        db.add(
            RuleChangeLog(
                rule_id=rule_id,
                checklist_type=checklist_type,
                action=action,
                before_state=before,
                after_state=after,
                operator=operator,
            )
        )

    @staticmethod
    def _snapshot(entity: Any) -> dict | None:
        """把 ORM 对象转成可 JSON 序列化的快照"""
        if entity is None:
            return None
        snapshot: dict[str, Any] = {}
        for column in entity.__table__.columns:
            value = getattr(entity, column.name)
            if isinstance(value, uuid.UUID):
                value = str(value)
            elif hasattr(value, "isoformat"):
                value = value.isoformat()
            snapshot[column.name] = value
        return snapshot

    @staticmethod
    def rule_to_dict(rule: ExtractionRule) -> dict:
        """规则 ORM 转响应字典"""
        return {
            "rule_id": str(rule.rule_id),
            "checklist_type": rule.checklist_type,
            "field_key": rule.field_key,
            "target_key": rule.field_key,
            "field_name_cn": rule.field_name_cn,
            "target_type": getattr(rule, "target_type", None) or "fact",
            "target_group_key": getattr(rule, "target_group_key", None),
            "target_group_name": getattr(rule, "target_group_name", None),
            "output_schema": getattr(rule, "output_schema", None) or {},
            "source_policy": getattr(rule, "source_policy", None) or {},
            "evidence_policy": getattr(rule, "evidence_policy", None) or {},
            "field_description": rule.field_description,
            "dimension_schema": rule.dimension_schema or {},
            "value_kind": rule.value_kind or "unknown",
            "data_type": rule.data_type,
            "unit": rule.unit,
            "required": rule.required,
            "multi_instance": rule.multi_instance,
            "merge_strategy": rule.merge_strategy,
            "strategy": rule.strategy or {},
            "validation": rule.validation,
            "fallback_value": rule.fallback_value,
            "priority": rule.priority,
            "enabled": rule.enabled,
            "version": rule.version,
            "created_at": rule.created_at,
            "updated_at": rule.updated_at,
        }


rule_repository = RuleRepository()
