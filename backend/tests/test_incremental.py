from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch
from uuid import uuid4

import pytest
from fastapi import HTTPException

from app.api.parse import incremental_extract, router
from app.models.contracts import ExtractedField
from app.models.database import ExtractedFieldModel
from app.rules.spec import RuleSpec
from app.schemas.parse import ExtractionRequest
from app.services.parse_service import ParseService, changed_field_keys, rule_snapshot


def service_fixture(rules):
    run_id, document_id = uuid4(), uuid4()
    run = SimpleNamespace(parse_run_id=run_id, document_id=document_id,
                          extraction_profile=rule_snapshot(rules, "gas"), requested_field_keys=None)
    document = SimpleNamespace(document_id=document_id, file_hash="hash", file_path="file.pdf",
                               file_name="file.pdf", checklist_type="gas", page_count=None)
    artifact = SimpleNamespace(artifact_id=uuid4(), pages=[{"page_number": 1, "text": "gas 4.5"}],
                               status="READY", quality={"weak_pages": []})
    db = Mock(get=AsyncMock(side_effect=lambda model, key: document if model.__name__ == "Document" else run),
              commit=AsyncMock(), rollback=AsyncMock(), add=Mock(), flush=AsyncMock())
    service = ParseService.__new__(ParseService)
    service.pdf_parser, service.ocr_client = Mock(), Mock()
    service._save_results = AsyncMock()
    return service, db, run, document, artifact


@pytest.mark.asyncio
async def test_full_cache_hit_never_downloads_or_preprocesses_and_uses_submission_snapshot():
    rule = RuleSpec(field_key="gas", field_name_cn="Gas", version=1)
    service, db, run, document, artifact = service_fixture([rule])
    with (patch("app.services.parse_service.artifact_service.find_ready", AsyncMock(return_value=artifact)),
          patch("app.services.parse_service.minio_client.download") as download,
          patch("app.services.parse_service.rule_repository.get_rules_for_document") as repository,
          patch("app.services.parse_service.rule_engine.extract_all", AsyncMock(return_value=([], []))) as engine):
        await service.parse_document(db, document.document_id, run.parse_run_id)
    download.assert_not_called()
    repository.assert_not_called()
    service.pdf_parser.extract_pages.assert_not_called()
    engine.assert_awaited_once()
    assert engine.call_args.args[1][0].version == 1
    assert run.cache_status == "ARTIFACT_HIT"


@pytest.mark.asyncio
async def test_explicit_source_artifact_reuse_keeps_correction_context_without_preprocessing():
    rule = RuleSpec(field_key="gas", field_name_cn="Gas")
    service, db, run, document, artifact = service_fixture([rule])
    artifact.document_id, artifact.file_hash = document.document_id, document.file_hash
    artifact.preprocess_profile_version = run.extraction_profile["preprocess_profile_version"]
    artifact.quality["source_corrections"] = [{"page_number": 1, "original_text": "12", "replacement_text": "173"}]
    run.extraction_profile["reuse_artifact_id"] = str(artifact.artifact_id)
    db.get = AsyncMock(side_effect=lambda model, key: artifact if model.__name__ == "DocumentArtifact" else document if model.__name__ == "Document" else run)
    service._extract = AsyncMock(return_value=([], [], None))
    with (patch("app.services.parse_service.artifact_service.find_ready") as lookup,
          patch("app.services.parse_service.minio_client.download") as download):
        await service.parse_document(db, document.document_id, run.parse_run_id)
    lookup.assert_not_called()
    download.assert_not_called()
    service.pdf_parser.extract_pages.assert_not_called()
    assert service._extract.call_args.args[-1]["source_corrections"][0]["replacement_text"] == "173"
    assert run.artifact_id == artifact.artifact_id


@pytest.mark.asyncio
async def test_incremental_replaces_only_selected_field_and_preserves_missing_and_parent_rows():
    rules = [RuleSpec(field_key=k, field_name_cn=k, required=k == "missing") for k in ["old", "new", "missing"]]
    service, db, run, document, artifact = service_fixture(rules)
    parent_id = uuid4()
    run.parent_parse_run_id, run.requested_field_keys = parent_id, ["new"]
    parent = SimpleNamespace(artifact_id=artifact.artifact_id, extraction_profile={
        **rule_snapshot(rules, "gas"), "details": [{"field_key": "missing", "required": True,
            "validation_status": "missing"}, {"field_key": "old", "validation_status": "passed"}]})
    old = ExtractedFieldModel(field_key="old", field_name="old", field_value=None,
                              extraction_method="keyword", confidence=0.5, instances=[{"value": "1"}, {"value": "2"}],
                              evidence=None, required=False, extraction_detail={"field_key": "old"})
    missing_row = ExtractedFieldModel(field_key="missing", field_name="missing", field_value=None,
                                      extraction_method="none", confidence=None, instances=[],
                                      required=True, validation_status="missing")
    db.get = AsyncMock(side_effect=lambda model, key: document if model.__name__ == "Document"
                       else parent if key == parent_id else run)
    db.execute = AsyncMock(return_value=Mock(scalars=Mock(return_value=Mock(all=Mock(return_value=[old, missing_row])))))
    new = ExtractedField(field_key="new", field_value="4.5", extraction_method="keyword", confidence=.99)
    with (patch("app.services.parse_service.artifact_service.find_ready", AsyncMock(return_value=artifact)),
          patch("app.services.parse_service.minio_client.download") as download,
          patch("app.services.parse_service.rule_engine.extract_all", AsyncMock(return_value=([new],
              [{"field_key": "new", "validation_status": "passed"}]))) as engine):
        await service.parse_document(db, document.document_id, run.parse_run_id)
    download.assert_not_called()
    assert [r.field_key for r in engine.call_args.args[1]] == ["new"]
    copies = [call.args[0] for call in db.add.call_args_list if isinstance(call.args[0], ExtractedFieldModel)]
    assert len(copies) == 2 and copies[0].field_key == "old"
    assert copies[0].field_value is None and len(copies[0].instances) == 2
    assert copies[0].extraction_detail["provenance"] == "inherited"
    assert old.extraction_detail == {"field_key": "old"}
    assert run.fields_extracted == 2
    assert run.checklist_type == document.checklist_type
    assert run.extraction_profile["missing_fields"][0]["field_key"] == "missing"
    assert run.quality_status != "QUALITY_OK"


def test_rule_delta_is_field_level_and_versions_are_recorded():
    old = [RuleSpec(field_key="gas", field_name_cn="Gas", version=1)]
    current = [RuleSpec(field_key="gas", field_name_cn="Gas", version=2), RuleSpec(field_key="new", field_name_cn="New")]
    assert changed_field_keys(current, rule_snapshot(old, "gas")) == ["gas", "new"]


@pytest.mark.asyncio
async def test_incremental_api_automatically_selects_changed_rules_and_pins_snapshot():
    document_id = uuid4()
    document = SimpleNamespace(document_id=document_id, checklist_type="gas", file_hash="hash")
    artifact = SimpleNamespace(artifact_id=uuid4(), status="READY")
    old = RuleSpec(field_key="old", field_name_cn="Old")
    new = RuleSpec(field_key="new", field_name_cn="New")
    parent = SimpleNamespace(parse_run_id=uuid4(), artifact_id=artifact.artifact_id,
                             extraction_profile=rule_snapshot([old], "gas"))
    added = []
    def add(run):
        run.parse_run_id = uuid4()
        added.append(run)
    db = Mock(get=AsyncMock(return_value=document), scalar=AsyncMock(return_value=parent),
              add=Mock(side_effect=add), commit=AsyncMock())
    with (patch("app.api.parse.rule_repository.get_rules_for_document", AsyncMock(return_value=[old, new])),
          patch("app.api.parse.artifact_service.find_ready", AsyncMock(return_value=artifact)),
          patch("app.api.parse.parse_document_task.delay") as submit):
        response = await incremental_extract(document_id, ExtractionRequest(), db)
    assert submit.call_args.args[2] == ["new"]
    assert response.mode == "INCREMENTAL" and response.artifact_id == artifact.artifact_id
    assert added[0].extraction_profile["rules"][1]["field_key"] == "new"


def test_incremental_endpoint_consumes_json_body():
    route = next(r for r in router.routes if r.path.endswith("/extract"))
    assert route.body_field is not None
    assert route.body_field.name == "payload"


@pytest.mark.asyncio
async def test_incremental_refuses_partial_or_stale_artifact_before_queue_submission():
    document_id = uuid4()
    db = Mock(get=AsyncMock(return_value=SimpleNamespace(checklist_type="gas", file_hash="hash")),
              scalar=AsyncMock(return_value=SimpleNamespace(parse_run_id=uuid4(), artifact_id=uuid4(),
                                                           extraction_profile={})))
    with (patch("app.api.parse.rule_repository.get_rules_for_document", AsyncMock(return_value=[RuleSpec(field_key="gas", field_name_cn="Gas")])),
          patch("app.api.parse.artifact_service.find_ready", AsyncMock(return_value=None)),
          patch("app.api.parse.parse_document_task.delay") as submit):
        with pytest.raises(HTTPException) as error:
            await incremental_extract(document_id, ExtractionRequest(field_keys=["gas"]), db)
    assert error.value.status_code == 409
    submit.assert_not_called()


@pytest.mark.asyncio
async def test_explicit_subset_does_not_claim_unexecuted_new_rule_and_drops_disabled_rows():
    old = RuleSpec(field_key="old", field_name_cn="Old")
    new = RuleSpec(field_key="new", field_name_cn="New")
    later = RuleSpec(field_key="later", field_name_cn="Later")
    disabled = RuleSpec(field_key="disabled", field_name_cn="Disabled")
    service, db, run, document, artifact = service_fixture([old, new, later])
    parent_id = uuid4()
    run.parent_parse_run_id, run.requested_field_keys = parent_id, ["new"]
    parent = SimpleNamespace(artifact_id=artifact.artifact_id,
                             extraction_profile=rule_snapshot([old, disabled], "gas"))
    stale = ExtractedFieldModel(field_key="disabled", field_name="Disabled", field_value="1", confidence=.5,
                                extraction_method="keyword", required=False)
    db.get = AsyncMock(side_effect=lambda model, key: document if model.__name__ == "Document" else parent if key == parent_id else run)
    db.execute = AsyncMock(return_value=Mock(scalars=Mock(return_value=Mock(all=Mock(return_value=[stale])))))
    with (patch("app.services.parse_service.artifact_service.find_ready", AsyncMock(return_value=artifact)),
          patch("app.services.parse_service.rule_engine.extract_all", AsyncMock(return_value=([], [])))):
        await service.parse_document(db, document.document_id, run.parse_run_id)
    assert not [c for c in db.add.call_args_list if isinstance(c.args[0], ExtractedFieldModel)]
    assert [r["field_key"] for r in run.extraction_profile["rules"]] == ["old", "new"]
    assert changed_field_keys([old, new, later], run.extraction_profile) == ["later"]


@pytest.mark.asyncio
async def test_rule_removal_alone_can_create_incremental_complete_view_and_force_refresh_is_full():
    document_id = uuid4()
    document = SimpleNamespace(checklist_type="gas", file_hash="hash")
    artifact = SimpleNamespace(artifact_id=uuid4(), status="READY")
    old, disabled = RuleSpec(field_key="old", field_name_cn="Old"), RuleSpec(field_key="disabled", field_name_cn="Disabled")
    parent = SimpleNamespace(parse_run_id=uuid4(), artifact_id=artifact.artifact_id,
                             extraction_profile=rule_snapshot([old, disabled], "gas"))
    def add(run):
        run.parse_run_id = uuid4()
    db = Mock(get=AsyncMock(return_value=document), scalar=AsyncMock(return_value=parent),
              add=Mock(side_effect=add), commit=AsyncMock())
    with (patch("app.api.parse.rule_repository.get_rules_for_document", AsyncMock(return_value=[old])),
          patch("app.api.parse.artifact_service.find_ready", AsyncMock(return_value=artifact)),
          patch("app.api.parse.parse_document_task.delay") as submit):
        response = await incremental_extract(document_id, ExtractionRequest(), db)
        assert response.mode == "INCREMENTAL" and submit.call_args.args[2] == []
        response = await incremental_extract(document_id, ExtractionRequest(field_keys=["old"], force_refresh=True), db)
        assert response.mode == "FULL" and submit.call_args.args[2] is None
        assert submit.call_args.args[3] is True
