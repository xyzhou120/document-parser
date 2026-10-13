from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch
from uuid import uuid4

import pytest
from fastapi import HTTPException

from app.api.documents import (
    ContextInput,
    DecisionInput,
    RelationInput,
    impact_plan,
    link_document,
    set_context,
    unlink_document,
)
from app.models.document_context import DocumentContext, DocumentContextAudit, DocumentRelationAudit
from app.services.document_service import plan_rule_impact, related_fact_candidates


def profile(*rules):
    return {"rules": list(rules), "extraction_engine": "rules"}


def test_impact_classifies_changes_but_reruns_every_changed_field():
    before = profile(
        {"field_key": "display", "field_name_cn": "旧名"},
        {"field_key": "validation", "validation": {"max": 5}},
        {"field_key": "semantic", "unit": "m"},
        {"field_key": "removed"},
        {"field_key": "same"},
    )
    after = profile(
        {"field_key": "display", "field_name_cn": "新名"},
        {"field_key": "validation", "validation": {"max": 6}},
        {"field_key": "semantic", "unit": "km"},
        {"field_key": "added"},
        {"field_key": "same"},
    )
    plan = plan_rule_impact(before, after)
    assert {item["field_key"]: item["category"] for item in plan["changed_fields"]} == {
        "display": "display",
        "validation": "validation",
        "semantic": "semantic",
    }
    assert plan["rerun_fields"] == ["added", "display", "semantic", "validation"]
    assert plan["inherited_fields"] == ["same"]
    assert plan["removed_fields"] == ["removed"]


def test_artifact_mismatch_invalidates_inheritance_even_when_rules_match():
    original = profile({"field_key": "count"})
    plan = plan_rule_impact(original, original, artifact_compatible=False)
    assert plan["suggested_mode"] == "FULL"
    assert plan["rerun_fields"] == ["count"]
    assert plan["inherited_fields"] == []


def test_agent_configuration_change_reruns_all_fields_without_forcing_ocr():
    original = profile({"field_key": "count"})
    plan = plan_rule_impact({**original, "agent_model": "old"}, {**original, "agent_model": "new"})
    assert plan["suggested_mode"] == "INCREMENTAL"
    assert plan["rerun_fields"] == ["count"]
    assert plan["configuration_changes"] == ["agent_model"]


def source(doc, value, **changes):
    fact = {
        "field_key": "count",
        "dimensions": {"workface": "A"},
        "value_kind": "actual",
        "source_role": "reported",
        "normalized": value,
        "unit": "个",
        **changes,
    }
    return {"document_id": doc, "file_name": doc + ".pdf", "source_role": "台账", "facts": [fact]}


def test_linked_same_identity_facts_remain_candidates_without_arbitration():
    result = related_fact_candidates([source("one", "120"), source("two", "173")])
    group = result["same_identity_candidates"][0]
    assert group["different_values"] is True
    assert group["resolution"] == "UNDECIDED"
    assert [row["fact"]["normalized"] for row in group["candidates"]] == ["120", "173"]


def test_different_or_unknown_identities_never_group_on_value_alone():
    result = related_fact_candidates(
        [
            source("one", "120"),
            source("two", "120", dimensions={"workface": "B"}),
            source("three", "120", dimensions={}),
        ]
    )
    assert not result["same_identity_candidates"]
    assert len(result["unmatched_facts"]) == 3


def test_same_named_fields_in_different_checklists_are_not_implicitly_equal():
    first, second = source("one", "120"), source("two", "120")
    first["checklist_type"], second["checklist_type"] = "monthly", "design"
    result = related_fact_candidates([first, second])
    assert not result["same_identity_candidates"]
    assert len(result["unmatched_facts"]) == 2


def test_context_and_relationship_require_explicit_actor_reason_role():
    with pytest.raises(ValueError):
        ContextInput(actor=" ", reason="ok")
    with pytest.raises(ValueError):
        RelationInput(actor="a", reason="ok", target_document_id=uuid4(), source_role="unknown")


@pytest.mark.asyncio
async def test_context_revision_conflict_prevents_append():
    document_id = uuid4()
    db = Mock(
        scalar=AsyncMock(
            side_effect=[SimpleNamespace(document_id=document_id), SimpleNamespace(revision=3)]
        ),
        add=Mock(),
    )
    with pytest.raises(HTTPException) as error:
        await set_context(
            document_id, ContextInput(actor="a", reason="核对", expected_revision=2), db
        )
    assert error.value.status_code == 409
    db.add.assert_not_called()


@pytest.mark.asyncio
async def test_context_save_appends_revision_and_audit_not_mutate_previous():
    document_id = uuid4()
    previous = DocumentContext(
        context_id=uuid4(),
        document_id=document_id,
        revision=1,
        subtype="report",
        context={"workface": "A"},
        actor="first",
        reason="首次",
        created_at=None,
    )
    db = Mock(
        scalar=AsyncMock(side_effect=[SimpleNamespace(document_id=document_id), previous]),
        add=Mock(),
        flush=AsyncMock(),
        commit=AsyncMock(),
    )
    result = await set_context(
        document_id,
        ContextInput(
            actor="second",
            reason="订正",
            expected_revision=1,
            subtype="ledger",
            context={"workface": "B"},
        ),
        db,
    )
    assert result["revision"] == 2
    assert previous.context == {"workface": "A"}
    assert isinstance(db.add.call_args_list[0].args[0], DocumentContext)
    assert isinstance(db.add.call_args_list[1].args[0], DocumentContextAudit)
    db.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_link_rejects_missing_or_different_mine():
    document_id, target_id = uuid4(), uuid4()
    documents = {
        document_id: SimpleNamespace(document_id=document_id, mine_id="mine-a"),
        target_id: SimpleNamespace(document_id=target_id, mine_id="mine-b"),
    }
    db = Mock(add=Mock())
    with patch(
        "app.api.documents.require_document",
        new=AsyncMock(side_effect=lambda db, id, **_: documents[id]),
    ):
        with pytest.raises(HTTPException) as error:
            await link_document(
                document_id,
                RelationInput(
                    target_document_id=target_id, actor="a", reason="关联", source_role="设计依据"
                ),
                db,
            )
    assert error.value.status_code == 422
    db.add.assert_not_called()


@pytest.mark.asyncio
async def test_relation_revision_conflict_prevents_link():
    document_id, target_id = uuid4(), uuid4()
    documents = {
        current: SimpleNamespace(document_id=current, mine_id="mine-a")
        for current in [document_id, target_id]
    }
    db = Mock(scalar=AsyncMock(return_value=4), add=Mock())
    with patch(
        "app.api.documents.require_document",
        new=AsyncMock(side_effect=lambda db, id, **_: documents[id]),
    ):
        with pytest.raises(HTTPException) as error:
            await link_document(
                document_id,
                RelationInput(
                    target_document_id=target_id,
                    actor="a",
                    reason="关联",
                    source_role="设计依据",
                    expected_revision=2,
                ),
                db,
            )
    assert error.value.status_code == 409
    db.add.assert_not_called()


@pytest.mark.asyncio
async def test_unlink_keeps_audit_and_advances_relation_revision():
    document_id, target_id = uuid4(), uuid4()
    row = SimpleNamespace(
        relation_id=uuid4(),
        source_document_id=document_id,
        target_document_id=target_id,
        source_role="设计依据",
        relation_type="attachment",
        actor="a",
        reason="旧关系",
        created_at=None,
    )
    db = Mock(
        scalar=AsyncMock(side_effect=[SimpleNamespace(document_id=document_id), 3, row]),
        add=Mock(),
        delete=AsyncMock(),
        commit=AsyncMock(),
    )
    result = await unlink_document(
        document_id,
        row.relation_id,
        DecisionInput(actor="b", reason="解除", expected_revision=3),
        db,
    )
    assert result["revision"] == 4
    audit = db.add.call_args.args[0]
    assert isinstance(audit, DocumentRelationAudit)
    assert audit.action == "UNLINK" and audit.snapshot["source_role"] == "设计依据"
    db.delete.assert_awaited_once_with(row)


@pytest.mark.asyncio
async def test_impact_rejects_wrong_preprocessor_version():
    document_id, artifact_id = uuid4(), uuid4()
    document = SimpleNamespace(document_id=document_id, file_hash="abc", checklist_type="monthly")
    parent = SimpleNamespace(
        parse_run_id=uuid4(),
        artifact_id=artifact_id,
        extraction_profile=profile({"field_key": "count"}),
    )
    artifact = SimpleNamespace(
        status="READY", document_id=document_id, file_hash="abc", preprocess_profile_version="old"
    )
    db = Mock(
        get=AsyncMock(side_effect=[document, artifact]), scalar=AsyncMock(return_value=parent)
    )
    with (
        patch(
            "app.api.documents.rule_repository.get_rules_for_document",
            new=AsyncMock(return_value=[]),
        ),
        patch("app.api.documents.rule_snapshot", return_value=profile({"field_key": "count"})),
        patch("app.api.documents.preprocessing_profile", return_value={"version": "current"}),
    ):
        result = await impact_plan(document_id, db)
    assert result["suggested_mode"] == "FULL"
    assert result["rerun_fields"] == ["count"]
