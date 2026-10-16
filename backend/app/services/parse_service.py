"""
解析服务 - 核心业务逻辑
协调 PDF 解析、OCR 识别、规则驱动的字段提取与质量评估
"""

import asyncio
import hashlib
import json
import logging
import os
from copy import deepcopy
from dataclasses import asdict
from dataclasses import fields as dataclass_fields
from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from time import perf_counter
from types import SimpleNamespace
from uuid import UUID

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models.contracts import ExtractedField
from app.models.database import Document, DocumentArtifact, ExtractedFieldModel, ParseLog, ParseRun
from app.parsers import PaddleOCRClient, PDFParser
from app.rules import RuleSpec, rule_engine, rule_repository
from app.services.artifact_service import artifact_service, preprocessing_profile
from app.services.evidence_images import EvidenceImages
from app.services.quality_inputs import quality_inputs_from_detail
from app.services.quality_service import assess_page_quality
from app.services.target_contract import target_status_for_state
from app.storage import minio_client

logger = logging.getLogger(__name__)


# 没有可用值的记录。方案 B 在旧版 missing 之外增加 incomplete（预算、轮次或输出问题）
# 和 not_found（证据完整地搜索过但不存在）。
INCOMPLETE_STATUSES = {"missing", "unresolved", "incomplete", "not_found"}


def knowledge_hash(rules: list[dict]) -> str:
    return hashlib.sha256(json.dumps(rules, sort_keys=True, ensure_ascii=False,
                                     default=str).encode()).hexdigest()


def rule_snapshot(rules: list[RuleSpec], checklist_type: str | None,
                  extraction_engine: str = "rules") -> dict:
    """固定提交时规则及前处理版本，排队任务不受后续规则编辑影响。"""
    profile = preprocessing_profile(extraction_engine)
    return {"checklist_type": checklist_type, "rule_count": len(rules),
            "extraction_engine": extraction_engine,
            "rules": [asdict(rule) for rule in rules],
            "knowledge_hash": knowledge_hash([asdict(rule) for rule in rules]),
            "preprocess_profile_version": profile["version"],
            "preprocess_manifest": profile["manifest"],
            **({"agent_config": settings.agent.model_dump(), "agent_model": {
                "model_name": settings.models["vision_llm"].model_name,
                "model_version": settings.models["vision_llm"].model_version,
                "base_url": settings.models["vision_llm"].base_url,
                "timeout": settings.models["vision_llm"].timeout,
                "trust_env": settings.models["vision_llm"].trust_env,
            }} if extraction_engine == "checklist_agent" else {})}


def snapshot_rules(profile: dict) -> list[RuleSpec]:
    names = {field.name for field in dataclass_fields(RuleSpec)}
    return [RuleSpec(**{k: v for k, v in rule.items() if k in names})
            for rule in profile.get("rules", [])]


def changed_field_keys(rules: list[RuleSpec], profile: dict) -> list[str]:
    old = {r["field_key"]: r for r in profile.get("rules", [])}
    return [r.field_key for r in rules if old.get(r.field_key) != asdict(r)]


class ParseService:
    """解析服务"""

    def __init__(self):
        self.pdf_parser = PDFParser()
        self.ocr_client = PaddleOCRClient()

    async def _record_output_event(self, db, run):
        if os.getenv("PLATFORM_INTEGRATION_ENABLED", "false").lower() in {"1", "true", "yes", "on"}:
            from app.services.integration_service import record_output_event
            await record_output_event(db, run)

    async def _extract(self, pages: list[dict], rules: list[RuleSpec],
                       extraction_engine: str, document, temp_dir: Path, profile: dict | None = None):
        """运行选定的提取器，并返回字段、详情和代理运行信息。"""
        if extraction_engine != "checklist_agent":
            fields, details = await rule_engine.extract_all(pages, rules)
            return fields, details, None
        from app.agents.checklist import ChecklistAgent

        source_path = temp_dir / "agent-source.pdf"
        existing_source = temp_dir / f"{document.document_id}.pdf"
        profile = profile or {}
        images = EvidenceImages(document, pages, temp_dir,
                                existing_source if existing_source.exists() else
                                (source_path if source_path.exists() else None),
                                dpi=profile.get("agent_config", {}).get("render_dpi"),
                                region_dpi=profile.get("agent_config", {}).get("region_render_dpi"))
        model_config = {**(profile.get("agent_model") or {
            "model_name": settings.models["vision_llm"].model_name,
            "model_version": settings.models["vision_llm"].model_version,
            "base_url": settings.models["vision_llm"].base_url,
            "timeout": settings.models["vision_llm"].timeout,
        }), "api_key": settings.models["vision_llm"].api_key, "max_retries": 0}
        agent_settings = {**profile.get("agent_config", settings.agent.model_dump()),
                          "source_corrections": profile.get("source_corrections", [])}
        agent = ChecklistAgent(settings_override=SimpleNamespace(**agent_settings),
                               model_config=model_config)
        fields, details, runtime = await agent.extract_all(pages, rules, images.get)
        runtime = {**(runtime or {}), "image_artifacts": images.receipts,
                   "source_corrections": profile.get("source_corrections", [])}
        if profile.get("source_corrections"):
            for detail in details:
                detail["source_corrections"] = profile["source_corrections"]
                detail.setdefault("issues", []).append({"code": "SOURCE_CORRECTION_CONTEXT",
                    "message": "原图未修改，人工订正作为独立来源上下文；自动候选仍需下游判断"})
        return fields, details, runtime

    async def _ocr_scanned_pages(
        self, db, parse_run_id, pages: list[dict], temp_path, region_ocr: bool,
    ) -> tuple[dict[int, list[dict]], set[int]]:
        """对原生文本层无法读取的页面执行 OCR（方案 C1）。

        规则引擎：对检查标记的页面执行行 OCR（/ocr）。
        清单代理：执行带像素框和布局标签的区域 OCR（/vl_ocr），使扫描附件可搜索并可裁剪核验。
        每个页面都会得到 ocr_status（not_needed / ok / failed），供可读性检查使用。
        """
        # 4. OCR（仅扫描页）
        ocr_blocks_by_page: dict[int, list[dict]] = {}
        ocr_failed_pages: set[int] = set()
        stage_start = perf_counter()
        for page in pages:
            page["ocr_status"] = "not_needed"
        # 方案 C1：清单代理通过区域 OCR 读取扫描页（文本带像素框和布局标签），
        # 使扫描附件可搜索且可裁剪。
        inspections = await asyncio.to_thread(self.pdf_parser.inspect_pages, str(temp_path))
        for inspection in inspections:
            if not inspection.needs_ocr:
                continue
            logger.info("OCR page %s", inspection.page_number)
            try:
                page_image = await asyncio.to_thread(
                    self.pdf_parser.get_page_image, str(temp_path), inspection.page_number
                )
                page_data = next(
                    (p for p in pages if p["page_number"] == inspection.page_number), None
                )
                if page_data is None:
                    continue
                import struct
                image_width, image_height = struct.unpack(">II", page_image[16:24])
                page_data["render"] = {"width": image_width, "height": image_height,
                                       "dpi": self.pdf_parser.dpi}
                recognize = (self.ocr_client.recognize_regions if region_ocr
                             else self.ocr_client.recognize)
                ocr_blocks = await recognize(
                    page_image, inspection.page_number, image_width, image_height)
                if not ocr_blocks:
                    page_data["ocr_status"] = "failed"
                    ocr_failed_pages.add(inspection.page_number)
                    await self._log(
                        db,
                        parse_run_id,
                        "WARNING",
                        "ocr",
                        f"第 {inspection.page_number} 页 OCR 未返回文本",
                        {"page": inspection.page_number},
                    )
                    continue
                offset = len(page_data.get("text") or "") + 1
                page_data.setdefault("text_blocks", [])
                for block in ocr_blocks:
                    bbox = None
                    if block.locator:
                        box = block.locator.bbox
                        bbox = [box.x0 * page_data["width"] / image_width,
                                box.y0 * page_data["height"] / image_height,
                                box.x1 * page_data["width"] / image_width,
                                box.y1 * page_data["height"] / image_height]
                    page_data["text_blocks"].append({
                        "text": block.text, "start_offset": offset,
                        "end_offset": offset + len(block.text), "bbox": bbox,
                        "source": "ocr", "coordinate_system": "PDF_POINT",
                        "label": getattr(block, "label", None),
                        "original_locator": block.locator.model_dump(mode="json") if block.locator else None,
                    })
                    offset += len(block.text) + 1
                ocr_text = "\n".join(block.text for block in ocr_blocks)
                page_data["text"] = (page_data["text"] or "") + "\n" + ocr_text
                page_data["ocr_status"] = "ok"
                ocr_blocks_by_page[inspection.page_number] = [
                    {"text": block.text, "confidence": block.confidence,
                     "locator": block.locator.model_dump(mode="json") if block.locator else None}
                    for block in ocr_blocks
                ]
            except Exception as exc:
                for failed_page in pages:
                    if failed_page["page_number"] == inspection.page_number:
                        failed_page["ocr_status"] = "failed"
                ocr_failed_pages.add(inspection.page_number)
                logger.error("OCR failed on page %s: %s", inspection.page_number, exc)
                await self._log(
                    db,
                    parse_run_id,
                    "ERROR",
                    "ocr",
                    f"第 {inspection.page_number} 页 OCR 失败: {exc}",
                )
        await self._log(
            db,
            parse_run_id,
            "INFO",
            "ocr",
            "OCR 阶段完成",
            {
                "duration_seconds": perf_counter() - stage_start,
                "failed_pages": sorted(ocr_failed_pages),
            },
        )

        return ocr_blocks_by_page, ocr_failed_pages

    async def parse_document(
        self, db: AsyncSession, document_id: UUID, parse_run_id: UUID,
        field_keys: list[str] | None = None, force_refresh: bool = False,
    ) -> None:
        """
        执行文档解析

        流程：加载文档 -> PDF 解析 -> 按需 OCR -> 取清单规则 -> 规则引擎提取 -> 落库
        """
        parse_run = await db.get(ParseRun, parse_run_id)
        if not parse_run:
            raise ValueError(f"ParseRun not found: {parse_run_id}")
        submitted_profile = deepcopy(getattr(parse_run, "extraction_profile", None) or {})
        if field_keys is None:
            field_keys = getattr(parse_run, "requested_field_keys", None)
        force_refresh = force_refresh or bool(submitted_profile.get("force_refresh"))

        parse_run.status = "RUNNING"
        parse_run.error_message = None
        parse_run.completed_at = None
        parse_run.duration_seconds = None
        parse_run.mean_confidence = None
        parse_run.quality_status = None
        parse_run.extraction_profile = submitted_profile
        parse_run.fields_extracted = 0
        parse_run.started_at = datetime.utcnow()
        await db.commit()

        temp_dir = TemporaryDirectory(prefix="document-parser-")
        try:
            # 1. 加载文档
            document = await db.get(Document, document_id)
            if not document:
                raise ValueError(f"Document not found: {document_id}")
            extraction_engine = submitted_profile.get("extraction_engine", "rules")
            profile_version = preprocessing_profile(extraction_engine)["version"]
            if submitted_profile.get("preprocess_profile_version", profile_version) != profile_version:
                raise ValueError("前处理版本已变更，请重新提交全量任务")
            rules = (snapshot_rules(submitted_profile) if "rules" in submitted_profile else
                     await rule_repository.get_rules_for_document(db, document))
            if not rules:
                raise ValueError(f"清单 {document.checklist_type} 未配置任何启用的提取规则")
            if not submitted_profile:
                submitted_profile = rule_snapshot(rules, document.checklist_type, extraction_engine)
            parse_run.extraction_profile = submitted_profile
            artifact = None

            # 仅字段运行有意创建新的不可变 ParseRun，复用父运行的页面索引和继承字段记录。
            # 图像缓存缺失时仍可下载来源，只渲染请求的页面或裁剪区域。
            if field_keys is not None and getattr(document, "file_hash", None):
                if force_refresh:
                    raise ValueError("增量运行不允许强制刷新前处理，请提交全量解析")
                artifact = await artifact_service.find_ready(
                    db, document_id, document.file_hash, profile_version
                )
                if artifact is None:
                    raise ValueError("无可复用的 READY 前处理产物，请先完成一次全量解析")
                parent_id = getattr(parse_run, "parent_parse_run_id", None)
                parent = None
                parent_fields = None
                if parent_id:
                    parent = await db.get(ParseRun, parent_id)
                    if parent is None or parent.artifact_id != artifact.artifact_id:
                        raise ValueError("父运行前处理版本与缓存不一致，请先提交全量解析")
                    parent_fields = await db.execute(
                        select(ExtractedFieldModel).where(ExtractedFieldModel.parse_run_id == parent_id)
                    )
                else:
                    parent_run = await db.scalar(
                        select(ParseRun).where(
                            ParseRun.document_id == document_id,
                            ParseRun.status == "COMPLETED",
                        ).order_by(ParseRun.created_at.desc()).limit(1)
                    )
                    parent_id = parent_run.parse_run_id if parent_run else None
                    parent = parent_run
                    if parent is None or parent.artifact_id != artifact.artifact_id:
                        raise ValueError("父运行前处理版本与缓存不一致，请先提交全量解析")
                    parent_fields = await db.execute(
                        select(ExtractedFieldModel).where(ExtractedFieldModel.parse_run_id == parent_id)
                    ) if parent_id else None
                active_keys = {r.field_key for r in rules}
                inherited = [x for x in (parent_fields.scalars().all() if parent_fields else [])
                             if x.field_key in active_keys]
                parse_run.parent_parse_run_id = parent_id
                parse_run.checklist_type = document.checklist_type
                parse_run.artifact_id = artifact.artifact_id
                parse_run.requested_field_keys = field_keys
                parse_run.run_mode = "INCREMENTAL"
                parse_run.cache_status = "ARTIFACT_HIT"
                selected = [r for r in rules if r.field_key in set(field_keys)]
                unknown = set(field_keys) - {r.field_key for r in rules}
                if unknown:
                    raise ValueError(f"字段没有启用规则: {sorted(unknown)}")
                fields, details, agent_runtime = await self._extract(
                    deepcopy(artifact.pages), selected,
                    submitted_profile.get("extraction_engine", "rules"), document, Path(temp_dir.name), submitted_profile
                )
                await self._save_results(db, document_id, parse_run_id, fields, details, selected, [])
                for old in inherited:
                    if old.field_key not in set(field_keys):
                        inherited_detail = {**(old.extraction_detail or {}), "provenance": "inherited",
                                            "inherited_from_parse_run_id": str(parent_id)}
                        db.add(ExtractedFieldModel(
                            parse_run_id=parse_run_id, document_id=document_id,
                            field_key=old.field_key, field_name=old.field_name,
                            field_value=old.field_value, extraction_method=old.extraction_method,
                            confidence=old.confidence, evidence=old.evidence,
                            data_type=old.data_type, unit=old.unit, required=old.required,
                            instances=old.instances, rule_id=old.rule_id,
                            rule_version=old.rule_version, validation_status=old.validation_status,
                            extraction_detail=inherited_detail,
                            quality_inputs=quality_inputs_from_detail(inherited_detail),
                        ))
                parse_run.status = "COMPLETED"
                parse_run.fields_extracted = len(fields) + len([
                    x for x in inherited if x.field_key not in set(field_keys)
                    and (x.instances or x.field_value is not None)
                    and x.validation_status not in {"missing", "blocked", "incomplete", "not_found"}
                ])
                parse_run.completed_at = datetime.utcnow()
                parent_profile = parent.extraction_profile or {}
                inherited_details = [deepcopy(d) for d in parent_profile.get("details", parent_profile.get("missing_fields", []))
                                     if d["field_key"] not in set(field_keys) and d["field_key"] in active_keys]
                detailed_keys = {d["field_key"] for d in inherited_details}
                inherited_details += [dict(x.extraction_detail or {}, field_key=x.field_key)
                                      for x in inherited if x.field_key not in set(field_keys) and x.field_key not in detailed_keys]
                full_details = [*inherited_details, *details]
                parse_run.quality_status = self._quality_status(None, parse_run.fields_extracted, len(rules),
                                                              (artifact.quality or {}).get("weak_pages", []), full_details)
                parse_run.duration_seconds = (parse_run.completed_at - parse_run.started_at).total_seconds()
                parent_rules = {r["field_key"]: r for r in parent_profile.get("rules", [])}
                effective_rules = [r if r["field_key"] in field_keys else parent_rules[r["field_key"]]
                                   for r in submitted_profile["rules"]
                                   if r["field_key"] in field_keys or r["field_key"] in parent_rules]
                parse_run.extraction_profile = {**submitted_profile, "rules": effective_rules, "details": full_details,
                    "knowledge_hash": knowledge_hash(effective_rules), "rule_count": len(effective_rules),
                    "missing_fields": [d for d in full_details if d.get("validation_status") in INCOMPLETE_STATUSES],
                    "artifact_status": artifact.status, "agent_runtime": agent_runtime,
                    "warnings": ["匹配分数不代表取值正确概率，字段未经人工确认"]}
                await self._record_output_event(db, parse_run)
                await db.commit()
                return

            # 完整重跑也可以复用 READY 产物。此短路径与旧版流程分开，
            # 防止缓存命中时重复预处理或调用 OCR；代理缺失的图像按需渲染。
            cached = None
            if submitted_profile.get("reuse_artifact_id"):
                cached = await db.get(DocumentArtifact, UUID(submitted_profile["reuse_artifact_id"]))
                if (cached is None or cached.document_id != document_id
                    or cached.file_hash != document.file_hash or cached.status != "READY"
                    or cached.preprocess_profile_version != profile_version):
                    raise ValueError("指定前处理版本不可复用；源文重算不能静默重建或改用其他版本")
            elif not force_refresh and getattr(document, "file_hash", None):
                cached = await artifact_service.find_ready(
                    db, document_id, document.file_hash, profile_version
                )
            if cached is not None:
                parse_run.cache_status = "ARTIFACT_HIT"
                parse_run.artifact_id = cached.artifact_id
                parse_run.run_mode = "FULL"
                parse_run.checklist_type = document.checklist_type
                cached_quality = cached.quality or {}
                cached_profile = {**submitted_profile, "artifact_id": str(cached.artifact_id)}
                if cached_quality.get("source_corrections"):
                    cached_profile["source_corrections"] = deepcopy(cached_quality["source_corrections"])
                    cached_profile["source_revision_chain"] = deepcopy(cached_quality.get("source_revision_chain", []))
                    cached_profile["source_review_status"] = cached_quality.get("source_review_status", "PARTIALLY_REVIEWED")
                parse_run.extraction_profile = cached_profile
                extracted_fields, details, agent_runtime = await self._extract(
                    cached.pages, rules, submitted_profile.get("extraction_engine", "rules"),
                    document, Path(temp_dir.name), cached_profile
                )
                await self._save_results(db, document_id, parse_run_id, extracted_fields, details, rules, [])
                parse_run.status = "COMPLETED"
                parse_run.completed_at = datetime.utcnow()
                parse_run.duration_seconds = (parse_run.completed_at - parse_run.started_at).total_seconds()
                parse_run.fields_extracted = len(extracted_fields)
                parse_run.mean_confidence = (
                    sum(f.confidence for f in extracted_fields) / len(extracted_fields)
                    if extracted_fields and extraction_engine != "checklist_agent" else None
                )
                parse_run.quality_status = self._quality_status(
                    parse_run.mean_confidence, len(extracted_fields), len(rules),
                    (cached.quality or {}).get("weak_pages", []), details)
                parse_run.extraction_profile = {**parse_run.extraction_profile,
                    "details": details, "artifact_status": cached.status,
                    "agent_runtime": agent_runtime,
                    "missing_fields": [d for d in details if d.get("validation_status") in INCOMPLETE_STATUSES],
                    "warnings": ["匹配分数不代表取值正确概率，字段未经人工确认"]}
                await self._record_output_event(db, parse_run)
                await db.commit()
                return

            # 2. 下载 PDF
            stage_start = perf_counter()
            pdf_data = await asyncio.to_thread(minio_client.download, document.file_path)
            if getattr(document, "file_hash", None) and hashlib.sha256(pdf_data).hexdigest() != document.file_hash:
                raise ValueError("文件内容与上传指纹不一致，停止解析")
            temp_path = Path(temp_dir.name) / f"{document_id}.pdf"
            temp_path.write_bytes(pdf_data)

            # 3. PDF 解析（含表格结构）
            logger.info("Parsing PDF: %s", document.file_name)
            pages = await asyncio.to_thread(self.pdf_parser.extract_pages, str(temp_path))
            document.page_count = len(pages)
            await self._log(
                db,
                parse_run_id,
                "INFO",
                "pdf_parse",
                f"解析 {len(pages)} 页",
                {"duration_seconds": perf_counter() - stage_start},
            )
            await db.commit()

            # 4. OCR（仅扫描页；清单引擎使用区域 OCR，见 _ocr_scanned_pages）
            region_ocr = extraction_engine == "checklist_agent"
            ocr_blocks_by_page, ocr_failed_pages = await self._ocr_scanned_pages(
                db, parse_run_id, pages, temp_path, region_ocr)
            # 5. 页面质量评估（真实 OCR 置信度，无 OCR 的页面不评估）
            page_quality_results = []
            if not region_ocr:
                for page_number, blocks in ocr_blocks_by_page.items():
                    quality = assess_page_quality(page_number, blocks)
                    page_quality_results.append(quality)
            # 区域 OCR 不返回置信度：对清单引擎来说只有失败页面属于弱页面，
            # 不能把每个扫描页都按未知的 1.0 计分。
            weak_pages = sorted(
                set(ocr_failed_pages)
                | {q.page_number for q in page_quality_results if q.status != "QUALITY_OK"}
            )

            if weak_pages:
                await self._log(
                    db,
                    parse_run_id,
                    "WARNING",
                    "quality",
                    f"{len(weak_pages)} 页 OCR 质量偏低",
                    {"pages": weak_pages[:20]},
                )

            # 持久化完整页面 JSON，包括原生文本范围、表格或单元格、OCR 块和质量信息。
            # PARTIAL 产物不能复用为 READY。
            if getattr(document, "file_hash", None):
                for page in pages:
                    page["ocr_blocks"] = ocr_blocks_by_page.get(page["page_number"], [])
                artifact = await artifact_service.save(
                    db, document_id, document.file_hash, profile_version,
                    pages, {"failed_pages": sorted(ocr_failed_pages), "weak_pages": weak_pages,
                            "pages": [q.model_dump(mode="json") for q in page_quality_results], "page_count": len(pages),
                            "ocr_policy": "region_ocr_plus_visual" if region_ocr else "ocr_gate",
                            "source_review_status": "UNVERIFIED"},
                    manifest=preprocessing_profile(extraction_engine)["manifest"],
                )
                parse_run.artifact_id = artifact.artifact_id
                parse_run.cache_status = "REFRESH" if force_refresh else "MISS"
            parse_run.run_mode = "FULL"

            # 6. 取该清单的规则

            # 7. 记录本次使用的规则快照，便于追溯
            parse_run.checklist_type = document.checklist_type
            parse_run.extraction_profile = {**submitted_profile, "extracted_at": datetime.utcnow().isoformat()}
            await db.commit()

            logger.info(
                "Extracting %s fields with checklist %s", len(rules), document.checklist_type
            )

            # 8. 规则引擎提取
            stage_start = perf_counter()
            extracted_fields, details, agent_runtime = await self._extract(
                pages, rules, submitted_profile.get("extraction_engine", "rules"),
                document, Path(temp_dir.name), submitted_profile
            )
            extraction_duration = perf_counter() - stage_start

            # 9. 落库
            await self._save_results(
                db, document_id, parse_run_id, extracted_fields, details, rules, weak_pages
            )

            # 10. 完成
            parse_run = await db.get(ParseRun, parse_run_id)
            if parse_run is None or parse_run.started_at is None:
                raise ValueError("解析任务记录不存在或缺少启动时间")
            parse_run.status = "COMPLETED"
            parse_run.completed_at = datetime.utcnow()
            parse_run.duration_seconds = (
                parse_run.completed_at - parse_run.started_at
            ).total_seconds()
            parse_run.fields_extracted = len(extracted_fields)
            if extracted_fields and extraction_engine != "checklist_agent":
                parse_run.mean_confidence = sum(f.confidence for f in extracted_fields) / len(
                    extracted_fields
                )
            parse_run.quality_status = self._quality_status(
                parse_run.mean_confidence, len(extracted_fields), len(rules), weak_pages, details
            )
            missing_fields = [
                d for d in details if d.get("validation_status") in INCOMPLETE_STATUSES or d.get("error")
            ]
            warnings = ["匹配分数不代表取值正确概率，字段未经人工确认"]
            if weak_pages:
                warnings.append(f"OCR 失败或质量偏低页：{', '.join(map(str, weak_pages))}")
            if any(d.get("validation_status") == "conflict" for d in details):
                warnings.append("部分字段在不同路径中取值冲突，请核对原文")
            if any(d.get("required") for d in missing_fields):
                warnings.append("存在必填字段未提取到，请人工核对")
            parse_run.extraction_profile = {
                **(parse_run.extraction_profile or {}),
                "missing_fields": missing_fields,
                "warnings": warnings,
                "details": details,
                "artifact_status": artifact.status if artifact is not None else None,
                "agent_runtime": agent_runtime,
            }

            await self._log(
                db,
                parse_run_id,
                "INFO",
                "extraction",
                f"提取 {len(extracted_fields)}/{len(rules)} 个字段",
                {
                    "missing": [
                        d["field_key"] for d in details if d.get("validation_status") == "missing"
                    ],
                    "weak_pages": weak_pages[:20],
                    "duration_seconds": extraction_duration,
                },
            )
            await self._record_output_event(db, parse_run)
            await db.commit()

            temp_path.unlink(missing_ok=True)

            logger.info(
                "Parse completed: %s/%s fields in %.2fs",
                len(extracted_fields),
                len(rules),
                parse_run.duration_seconds,
            )

        except Exception as e:
            await db.rollback()
            parse_run = await db.get(ParseRun, parse_run_id)
            if parse_run:
                parse_run.status = "FAILED"
                parse_run.error_message = str(e)
                await self._log(db, parse_run_id, "ERROR", "pipeline", str(e))
                await db.commit()

            logger.error("Parse failed: %s", e, exc_info=True)
            raise
        finally:
            temp_dir.cleanup()

    # ---- 内部方法 ----

    @staticmethod
    def _quality_status(
        mean_confidence: float | None,
        extracted: int,
        total: int,
        weak_pages: list[int],
        details: list[dict] | None = None,
    ) -> str:
        """质量状态描述证据完整性；未经校准的路径分数不能证明正确性。"""
        if extracted == 0:
            return "FAILED"
        coverage = extracted / total if total else 0

        if coverage >= 0.6 and not weak_pages:
            return "REVIEW_REQUIRED"
        return "DEGRADED"

    async def _save_results(
        self,
        db: AsyncSession,
        document_id: UUID,
        parse_run_id: UUID,
        extracted_fields: list[ExtractedField],
        details: list[dict],
        rules: list[RuleSpec],
        weak_pages: list[int],
    ) -> None:
        """保存提取结果，附实例明细与规则版本"""
        await db.execute(
            delete(ExtractedFieldModel).where(ExtractedFieldModel.parse_run_id == parse_run_id)
        )

        detail_by_key = {d["field_key"]: d for d in details}
        rule_by_key = {r.field_key: r for r in rules}

        for field in extracted_fields:
            detail = detail_by_key.get(field.field_key) or {}
            rule = rule_by_key.get(field.field_key)
            instances = detail.get("instances") or []

            db_field = ExtractedFieldModel(
                parse_run_id=parse_run_id,
                document_id=document_id,
                field_key=field.field_key,
                field_name=detail.get("field_name")
                or (rule.field_name_cn if rule else field.field_key),
                target_type=(rule.target_type if rule else "fact"),
                target_group_key=(rule.target_group_key if rule else None),
                target_status=target_status_for_state(detail.get("validation_status")),
                status_reason={"validation_status": detail.get("validation_status") or "passed"},
                field_value=field.field_value,
                extraction_method=field.extraction_method,
                confidence=None if field.extraction_method == "agent_extract" else field.confidence,
                evidence=(
                    {
                        "quote": field.evidence.quote,
                        "page_number": field.evidence.page_number,
                        "confidence": field.evidence.confidence,
                        "locator": field.evidence.locator.model_dump(mode="json") if field.evidence.locator else None,
                        "location_status": "available" if field.evidence.locator else "unavailable",
                    }
                    if field.evidence
                    else None
                ),
                data_type=detail.get("data_type") or (rule.data_type if rule else None),
                unit=detail.get("unit") if field.extraction_method == "agent_extract" else
                     (detail.get("unit") or (rule.unit if rule else None)),
                required=bool(detail.get("required", rule.required if rule else False)),
                instances=instances or None,
                rule_id=UUID(rule.rule_id) if rule and rule.rule_id else None,
                rule_version=detail.get("rule_version") or (rule.version if rule else None),
                validation_status=detail.get("validation_status") or "passed",
                extraction_detail=detail or None,
                quality_inputs=quality_inputs_from_detail(detail),
            )
            db.add(db_field)

        # 将缺失或阻断的规则记录持久化为明确的空事实。省略记录会让 API 看起来像是从未请求
        # 该规则，并阻止下游服务区分缺失和待处理状态。
        extracted_keys = {field.field_key for field in extracted_fields}
        for detail in details:
            field_key = detail.get("field_key")
            if not field_key or field_key in extracted_keys:
                continue
            rule = rule_by_key.get(field_key)
            db.add(ExtractedFieldModel(
                parse_run_id=parse_run_id,
                document_id=document_id,
                field_key=field_key,
                field_name=detail.get("field_name") or (rule.field_name_cn if rule else field_key),
                target_type=(rule.target_type if rule else "fact"),
                target_group_key=(rule.target_group_key if rule else None),
                target_status=target_status_for_state(detail.get("validation_status")),
                status_reason={"validation_status": detail.get("validation_status") or "missing"},
                field_value=None,
                extraction_method=detail.get("extraction_method") or "none",
                confidence=None,
                evidence=None,
                data_type=detail.get("data_type") or (rule.data_type if rule else None),
                unit=detail.get("unit") or (rule.unit if rule else None),
                required=bool(detail.get("required", rule.required if rule else False)),
                instances=detail.get("instances") or None,
                rule_id=UUID(rule.rule_id) if rule and rule.rule_id else None,
                rule_version=detail.get("rule_version") or (rule.version if rule else None),
                validation_status=detail.get("validation_status") or "missing",
                extraction_detail=detail or None,
                quality_inputs=quality_inputs_from_detail(detail),
            ))

        await db.flush()

    @staticmethod
    async def _log(
        db: AsyncSession,
        parse_run_id: UUID,
        level: str,
        stage: str,
        message: str,
        details: dict | None = None,
    ) -> None:
        """写入解析日志"""
        db.add(
            ParseLog(
                parse_run_id=parse_run_id,
                level=level,
                stage=stage,
                message=message,
                details=details,
            )
        )
