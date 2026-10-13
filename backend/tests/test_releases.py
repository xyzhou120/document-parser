"""使用真实本地 SQLite 事务测试发布生命周期，无模型或网络调用。"""

from copy import deepcopy
from dataclasses import asdict
from uuid import uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine, select
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import Session

from app.models.database import Base, ParseRun
from app.models.releases import RuleReleaseEvent
from app.models.rules import ChecklistType, ExtractionRule
from app.rules.repository import RuleRepository
from app.schemas.releases import DraftBundlePayload, ReleaseCommand
from app.services.parse_service import knowledge_hash, rule_snapshot, snapshot_rules
from app.services.release_service import ReleaseService, bundle_specs, diff_bundles, validate_bundle


class LocalDB:
    def __init__(self, session):
        self.session = session

    def add(self, row):
        self.session.add(row)

    async def get(self, model, key):
        return self.session.get(model, key)

    async def execute(self, statement):
        return self.session.execute(statement)

    async def scalar(self, statement):
        return self.session.scalar(statement)

    async def flush(self):
        self.session.flush()


@pytest.fixture
def db():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        session.add(ChecklistType(checklist_type="alpha", checklist_name="测试清单", is_active=True))
        session.flush()
        yield LocalDB(session)
    engine.dispose()


def rule(value="原规则", **overrides):
    return {"field_key": "gas_count", "field_name_cn": value,
            "strategy": {"methods": ["agent_extract"], "agent_extract": {"instructions": value}},
            "multi_instance": True, **overrides}


def command(generation, **kwargs):
    return ReleaseCommand(expected_generation=generation, operator="reviewer",
                          reason="显式发布决定", **kwargs)


async def draft(db, rows=None):
    return await ReleaseService().create_draft(db, DraftBundlePayload(
        checklist_type="alpha", rules=rows if rows is not None else [rule()], operator="editor"))


@pytest.mark.asyncio
async def test_publish_snapshot_edit_isolation_rollback_and_audit(db):
    service = ReleaseService()
    first = await draft(db)
    immutable_rules = deepcopy(first.rules)
    assert validate_bundle(first)["valid"]
    profile = rule_snapshot(bundle_specs(first), "alpha", "rules")
    assert profile["knowledge_hash"] == first.knowledge_hash
    first_event = await service.activate(db, first.bundle_id, command(0))
    assert first_event.action == "PUBLISH" and first_event.generation == 1
    second = await draft(db, [rule("新规则")])
    await service.activate(db, second.bundle_id, command(1))
    repository = RuleRepository()
    assert (await repository.get_rules_for_checklist(db, "alpha"))[0].field_name_cn == "新规则"
    assert snapshot_rules(profile)[0].field_name_cn == "原规则"
    rollback = await service.activate(db, first.bundle_id, command(2), rollback=True)
    assert rollback.generation == 3 and rollback.previous_bundle_id == second.bundle_id
    assert (await repository.get_rules_for_checklist(db, "alpha"))[0].field_name_cn == "原规则"
    events = db.session.scalars(select(RuleReleaseEvent)).all()
    assert [e.action for e in events] == ["DRAFT", "PUBLISH", "DRAFT", "PUBLISH", "ROLLBACK"]
    assert first.rules == immutable_rules


@pytest.mark.asyncio
async def test_stale_writer_and_stale_draft_are_rejected_without_mutation(db):
    service = ReleaseService()
    first = await draft(db)
    stale_draft = await draft(db, [rule("并发草稿")])
    await service.activate(db, first.bundle_id, command(0))
    for cmd in [command(0), command(1)]:
        with pytest.raises(HTTPException) as error:
            await service.activate(db, stale_draft.bundle_id, cmd)
        assert error.value.status_code == 409
    head = await service.head(db, "alpha")
    assert head.generation == 1 and head.bundle_id == first.bundle_id
    assert len(db.session.scalars(select(RuleReleaseEvent)).all()) == 3


@pytest.mark.asyncio
async def test_unpublished_rollback_and_inactive_checklist_rejected(db):
    service = ReleaseService()
    bundle = await draft(db)
    with pytest.raises(HTTPException) as error:
        await service.activate(db, bundle.bundle_id, command(0), rollback=True)
    assert error.value.status_code == 409
    db.session.get(ChecklistType, "alpha").is_active = False
    await db.flush()
    assert await RuleRepository().get_rules_for_checklist(db, "alpha") == []
    with pytest.raises(HTTPException) as error:
        await service.activate(db, bundle.bundle_id, command(0))
    assert error.value.status_code == 409


@pytest.mark.asyncio
async def test_sample_binding_requires_matching_completed_snapshot(db):
    service = ReleaseService()
    bundle = await draft(db)
    run = ParseRun(parse_run_id=uuid4(), document_id=uuid4(), checklist_type="alpha",
                   status="COMPLETED", extraction_profile={"knowledge_hash": "wrong"})
    db.add(run)
    await db.flush()
    with pytest.raises(HTTPException) as error:
        await service.activate(db, bundle.bundle_id, command(0, sample_run_ids=[run.parse_run_id]))
    assert error.value.status_code == 422
    run.extraction_profile = {"knowledge_hash": bundle.knowledge_hash}
    event = await service.activate(db, bundle.bundle_id, command(
        0, sample_run_ids=[run.parse_run_id], sample_report={"claim": "100%"}))
    assert event.sample_runs[0]["verification_status"] == "UNVERIFIED"
    assert event.sample_report["verified_by_platform"] is False
    assert event.sample_report["report"] == {"claim": "100%"}


@pytest.mark.asyncio
async def test_preview_diff_validation_and_no_active_rules(db):
    service = ReleaseService()
    first = await draft(db)
    second = await draft(db, [rule("新规则"), rule(field_key="gas_pressure", enabled=False)])
    preview = await service.preview(db, second, first.bundle_id)
    assert preview["model_called"] is False
    assert preview["validation"]["verification_status"] == "UNVERIFIED"
    assert preview["diff"]["added"] == ["gas_pressure"]
    assert preview["diff"]["changed"][0]["field_key"] == "gas_count"
    assert len(preview["runtime_rules"]) == 1
    assert knowledge_hash(preview["runtime_rules"]) == second.knowledge_hash
    empty = await draft(db, [rule(enabled=False)])
    with pytest.raises(HTTPException) as error:
        await service.activate(db, empty.bundle_id, command(0))
    assert error.value.status_code == 422
    assert not validate_bundle(empty)["valid"]
    assert diff_bundles(first, None)["added"] == ["gas_count"]


@pytest.mark.asyncio
async def test_crud_source_snapshot_and_no_publication_compatibility(db):
    edit = ExtractionRule(rule_id=uuid4(), checklist_type="alpha", field_key="gas_count",
                          field_name_cn="编辑原值", strategy={}, data_type="string",
                          enabled=True, version=1, priority=50, required=False,
                          multi_instance=False, merge_strategy="value_consistent")
    db.add(edit)
    await db.flush()
    repo = RuleRepository()
    assert (await repo.get_rules_for_checklist(db, "alpha"))[0].field_name_cn == "编辑原值"
    bundle = await ReleaseService().create_draft(db, DraftBundlePayload(
        checklist_type="alpha", operator="editor"))
    await ReleaseService().activate(db, bundle.bundle_id, command(0))
    edit.field_name_cn = "尚未发布的编辑"
    await db.flush()
    assert (await repo.get_rules_for_checklist(db, "alpha"))[0].field_name_cn == "编辑原值"
    assert asdict(bundle_specs(bundle)[0])["rule_id"] == str(edit.rule_id)


@pytest.mark.asyncio
async def test_duplicate_invalid_cross_checklist_and_missing_bundle_rejected(db):
    for rows in [[rule(), rule()], [rule(data_type="invalid")], [rule(checklist_type="beta")]]:
        with pytest.raises(HTTPException) as error:
            await draft(db, rows)
        assert error.value.status_code == 422
    with pytest.raises(HTTPException) as error:
        await ReleaseService().get(db, uuid4())
    assert error.value.status_code == 404


@pytest.mark.asyncio
async def test_immutable_bundle_and_audit_orm_guards(db):
    bundle = await draft(db)
    bundle.note = "修改历史"
    with pytest.raises(ValueError, match="append-only"):
        await db.flush()
    db.session.rollback()


@pytest.mark.asyncio
async def test_publication_uses_checklist_row_lock(db):
    statements = []
    original = db.execute

    async def record(statement):
        statements.append(str(statement.compile(dialect=postgresql.dialect())))
        return await original(statement)

    db.execute = record
    await draft(db)
    assert any("checklist_types" in sql and "FOR UPDATE" in sql for sql in statements)


@pytest.mark.asyncio
async def test_corrupt_active_bundle_never_falls_back_to_editing_rules(db):
    bundle = await draft(db)
    await ReleaseService().activate(db, bundle.bundle_id, command(0))
    # 模拟损坏的持久化 JSON，不绕过 ORM 防护执行刷新。
    bundle.rules[0]["field_name_cn"] = "损坏"
    with pytest.raises(ValueError, match="校验失败"):
        await RuleRepository().get_rules_for_checklist(db, "alpha")
