"""在没有 API 和队列的情况下，对本地 PDF 样本运行生产清单代理。

从仓库根目录执行：
  backend/.venv/Scripts/python.exe backend/scripts/agent_sample_runner.py \
    --manifest output/agent_evaluation/manifest.json --ids 131 \
    --fields actual_hole_count --out-dir output/agent_evaluation/runs/smoke

原生文本和表格提取在本地完成；只有代理请求时，原始 PDF 图像才会发送到配置的
视觉模型。--dry-run 不调用模型。
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import math
import subprocess
import sys
import tempfile
import time
from dataclasses import fields as dataclass_fields
from datetime import UTC, datetime
from pathlib import Path, PureWindowsPath
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pymupdf

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))

from app.agents.checklist import AgentLimits, ChecklistAgent  # noqa: E402
from app.parsers.pdf_parser import PDFParser  # noqa: E402
from app.rules.agent_knowledge import CHECKLIST, agent_seed_rules  # noqa: E402
from app.rules.spec import RuleSpec  # noqa: E402


def seed_specs(field_keys: list[str] | None = None) -> tuple[list[RuleSpec], str]:
    seeds = agent_seed_rules()
    allowed = {r["field_key"] for r in seeds}
    unknown = set(field_keys or []) - allowed
    if unknown:
        raise ValueError("Unknown field keys: " + ", ".join(sorted(unknown)))
    requested = set(field_keys) if field_keys else {r["field_key"] for r in seeds if r["enabled"]}
    accepted_keys = {f.name for f in dataclass_fields(RuleSpec)}
    specs = [RuleSpec(**{k: v for k, v in row.items() if k in accepted_keys}, source="agent_seed")
             for row in seeds if row["field_key"] in requested]
    digest = hashlib.sha256(json.dumps(seeds, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    return specs, digest


def _safe_manifest_file(manifest_path: Path, relative: str, sample_root: Path | None) -> Path:
    """解析复制后的样本，不能静默逃出包根目录。"""
    base = (sample_root or manifest_path.parent).resolve()
    if Path(relative).is_absolute() or PureWindowsPath(relative).is_absolute():
        raise ValueError(f"Manifest copied_file must be relative: {relative}")
    relative = relative.replace("\\", "/")
    candidate = (base / relative).resolve()
    try:
        candidate.relative_to(base)
    except ValueError as exc:
        raise ValueError(f"Manifest copied_file escapes sample root: {relative}") from exc
    return candidate


def sources_from_manifest(
    manifest_path: Path,
    ids: list[int],
    root: Path = ROOT,
    sample_root: Path | None = None,
) -> list[dict]:
    manifest_path = manifest_path.resolve()
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    by_id = {int(d["id"]): d for d in manifest["documents"]}
    if len(by_id) != len(manifest["documents"]):
        raise ValueError("Duplicate sample ids in manifest")
    if len({d.get("sha256") for d in manifest["documents"]}) != len(by_id):
        raise ValueError("Duplicate source hashes in manifest")
    missing = set(ids) - set(by_id)
    if missing:
        raise ValueError("Unknown sample ids: " + ", ".join(map(str, sorted(missing))))
    if len(set(ids)) != len(ids):
        raise ValueError("Duplicate requested ids")
    sources = []
    for sample_id in ids:
        row = by_id[sample_id]
        copied_file = row.get("copied_file")
        if copied_file:
            path = _safe_manifest_file(manifest_path, copied_file, sample_root)
            resolution = "copied_file"
        else:
            # 旧版清单早于自包含包。保留其仓库根目录路径行为，
            # 但明确记录选定的根目录。
            path = (sample_root or root) / row["path"].replace("\\", "/")
            resolution = "legacy_path"
        if not path.is_file():
            raise ValueError(f"Sample file missing for id {sample_id}: {path}")
        if hashlib.sha256(path.read_bytes()).hexdigest() != row["sha256"]:
            raise ValueError(f"Source hash mismatch for id {sample_id}: {path}")
        expected_pages = row.get("pages")
        if expected_pages is not None:
            try:
                with pymupdf.open(path) as document:
                    actual_pages = len(document)
            except Exception as exc:
                raise ValueError(f"Sample PDF unreadable for id {sample_id}: {path}") from exc
            if actual_pages != expected_pages:
                raise ValueError(f"Sample page count mismatch for id {sample_id}: {actual_pages} != {expected_pages}")
        sources.append({"id": sample_id, "path": str(path),
                        "expected_hash": row["sha256"], "split": row.get("split"),
                        "mine": row.get("mine"), "layout": row.get("layout"),
                        "resolution": resolution})
    return sources


def freeze_metadata(
    sources: list[dict], rules: list[RuleSpec], knowledge_hash: str,
    limits: dict, config: dict | None, manifest_path: Path | None, dpi: int,
) -> dict:
    try:
        revision = subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT,
                                  capture_output=True, text=True, check=False).stdout.strip() or None
    except OSError:
        revision = None
    # 只对可安全审计的模型配置计算哈希，不能把凭据写入报告。
    model_settings = {key: (config or {}).get(key) for key in (
        "model_name", "model_version", "temperature", "max_tokens", "max_retries")}
    return {
        "git_revision": revision,
        "model": model_settings["model_name"], "model_version": model_settings["model_version"],
        "model_config_hash": hashlib.sha256(json.dumps(model_settings, sort_keys=True).encode()).hexdigest(),
        "knowledge_hash": knowledge_hash,
        "manifest_hash": hashlib.sha256(manifest_path.read_bytes()).hexdigest() if manifest_path else None,
        "sources": [{"id": s["id"], "file_hash": s.get("expected_hash") or hashlib.sha256(Path(s["path"]).read_bytes()).hexdigest(), "split": s.get("split")} for s in sources],
        "field_keys": [r.field_key for r in rules], "limits": limits,
        "artifact_profile": {"parser": "PDFParser_native", "pymupdf": pymupdf.VersionBind, "render_dpi": dpi},
    }


class LocalPDFImages:
    """不可变的已核验 PDF 快照；来源与渲染证据不能发生漂移。"""

    def __init__(self, source: Path, expected_hash: str | None = None, dpi: int = 180):
        if not 72 <= dpi <= 300:
            raise ValueError("dpi must be between 72 and 300")
        self.pdf_bytes = source.read_bytes()
        self.file_hash = hashlib.sha256(self.pdf_bytes).hexdigest()
        if expected_hash is not None and self.file_hash != expected_hash:
            raise ValueError(f"Source hash mismatch: {source.name}")
        self._doc = pymupdf.open(stream=self.pdf_bytes, filetype="pdf")
        if self._doc.needs_pass or not len(self._doc):
            self._doc.close()
            raise ValueError("Source PDF is encrypted or contains no pages")
        self.dpi = dpi
        self.receipts: list[dict] = []

    async def get(self, page_number: int, bbox: list[float] | None) -> bytes:
        if isinstance(page_number, bool) or not isinstance(page_number, int) or not 1 <= page_number <= len(self._doc):
            raise ValueError("Invalid page number")
        page = self._doc[page_number - 1]
        rect = page.rect
        if bbox is not None:
            if (len(bbox) != 4 or not all(not isinstance(v, bool) and isinstance(v, int | float) and math.isfinite(v) for v in bbox)
                    or bbox[0] < 0 or bbox[1] < 0 or bbox[2] <= bbox[0] or bbox[3] <= bbox[1]
                    or bbox[2] > rect.width or bbox[3] > rect.height):
                raise ValueError("Invalid PDF-point region")
            rect = pymupdf.Rect(bbox)
        image = page.get_pixmap(dpi=self.dpi, clip=rect, alpha=False).tobytes("png")
        self.receipts.append({"file_hash": self.file_hash, "page_number": page_number,
                              "bbox": bbox, "coordinate_system": "PDF_POINT",
                              "dpi": self.dpi, "image_hash": hashlib.sha256(image).hexdigest(),
                              "bytes": len(image), "source": "verified_local_original_pdf"})
        return image

    def close(self) -> None:
        self._doc.close()


async def run_sample(
    source: dict,
    rules: list[RuleSpec],
    knowledge_hash: str,
    limits: dict,
    model_config: Any = None,
    client: Any = None,
    dpi: int = 180,
    dry_run: bool = False,
) -> dict:
    started = time.monotonic()
    image_source = LocalPDFImages(Path(source["path"]), source.get("expected_hash"), dpi=dpi)
    try:
        # 解析供每次视觉工具响应使用的同一份不可变字节。
        # 来源校验后不能重新读取原始路径。
        with tempfile.TemporaryDirectory(prefix="agent-sample-") as scratch:
            snapshot = Path(scratch) / "source.pdf"
            snapshot.write_bytes(image_source.pdf_bytes)
            pages = PDFParser().extract_pages(str(snapshot))
        settings_override = SimpleNamespace(**limits)
        if dry_run:
            fields, details = [], []
            runtime = {"status": "DRY_RUN", "failures": [], "coverage": {
                "visited_pages": [], "unvisited_pages": [p["page_number"] for p in pages]}}
        else:
            agent = ChecklistAgent(settings_override=settings_override, client=client,
                                   model_config=model_config)
            fields, details, runtime = await agent.extract_all(pages, rules, image_source.get)
        runtime["image_artifacts"] = image_source.receipts
        result = {
            "schema_version": 1, "document_id": source["id"], "sample_id": source["id"],
            "document_name": Path(source["path"]).name, "source_path": source["path"],
            "file_hash": image_source.file_hash, "checklist_type": CHECKLIST["checklist_type"],
            "status": runtime.get("status"), "pages": len(pages),
            "fields": [field.model_dump(mode="json") for field in fields],
            "details": details, "agent_runtime": runtime,
            "run_metadata": {
                "started_at": datetime.now(UTC).isoformat(),
                "duration_seconds": round(time.monotonic() - started, 3),
                "knowledge_hash": knowledge_hash, "knowledge_source": "agent_seed_rules",
                "field_keys": [r.field_key for r in rules], "file_hash": image_source.file_hash,
                "split": source.get("split"), "model": runtime.get("model"),
                "model_version": runtime.get("model_version"), "limits": limits,
                "preprocessing": "PDFParser native text/tables; no remote OCR; requested original-image views",
                "pymupdf_version": pymupdf.VersionBind, "render_dpi": dpi,
                "token_usage": runtime.get("token_usage"), "cost": None,
                "accuracy_status": "UNVERIFIED", "dry_run": dry_run,
            },
            "page_bounds": [{"page_number": i + 1, "width": p.rect.width, "height": p.rect.height}
                            for i, p in enumerate(image_source._doc)],
        }
        return result
    finally:
        image_source.close()


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    src = parser.add_mutually_exclusive_group(required=True)
    src.add_argument("--manifest", type=Path)
    src.add_argument("--pdf", type=Path)
    parser.add_argument("--ids", type=int, nargs="+")
    parser.add_argument("--sample-root", type=Path,
                        help="base directory for manifest copied_file entries (defaults to manifest directory)")
    parser.add_argument("--document-id", type=int, default=0, help="id for --pdf, use manifest id to import results")
    parser.add_argument("--sha256", help="optional expected hash for --pdf; manifest mode requires stored hash")
    parser.add_argument("--fields", nargs="+", help="seed field keys, including disabled fields when explicit")
    parser.add_argument("--out-dir", type=Path, required=True)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--purpose", choices=["development", "holdout"], default="development")
    parser.add_argument("--run-id", help="audit identity; generated when omitted")
    parser.add_argument("--repeat-index", type=int, choices=[1, 2, 3], default=1)
    parser.add_argument("--freeze-record", type=Path,
                        help="create frozen configuration or require exact equality with existing record")
    parser.add_argument("--dpi", type=int, default=180)
    parser.add_argument("--max-rounds", type=int, default=40)
    parser.add_argument("--max-rounds-per-batch", type=int, default=6)
    parser.add_argument("--max-actions-per-round", type=int, default=4)
    parser.add_argument("--max-view-pages", type=int, default=6)
    parser.add_argument("--max-region-views", type=int, default=8)
    parser.add_argument("--max-tokens", type=int, default=2000)
    parser.add_argument("--max-total-tokens", type=int, default=200000)
    parser.add_argument("--field-batch-size", type=int, default=3)
    parser.add_argument("--max-context-chars", type=int, default=24000)
    parser.add_argument("--max-search-results", type=int, default=8)
    parser.add_argument("--model", help="override configured VISION_LLM model name")
    parser.add_argument("--model-version", help="deployment revision for audit")
    args = parser.parse_args(argv)
    if args.manifest and not args.ids:
        parser.error("--manifest requires explicit --ids; whole-corpus remote calls are never implicit")
    if args.pdf and args.ids:
        parser.error("--ids applies only to --manifest")
    if args.pdf and args.sample_root:
        parser.error("--sample-root applies only to --manifest")
    if not 72 <= args.dpi <= 300:
        parser.error("--dpi must be between 72 and 300")
    for name in AgentLimits.__dataclass_fields__:
        if getattr(args, name, 1) < 1:
            parser.error(f"--{name.replace('_', '-')} must be positive")
    return args


async def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    rules, knowledge_hash = seed_specs(args.fields)
    sources = sources_from_manifest(args.manifest, args.ids, sample_root=args.sample_root) if args.manifest else [{
        "id": args.document_id, "path": str(args.pdf.resolve()), "expected_hash": args.sha256}]
    limits = {name: getattr(args, name) for name in AgentLimits.__dataclass_fields__}
    config = None
    if not args.dry_run:
        from app.config import settings

        config = settings.models["vision_llm"].model_dump()
        config["max_retries"] = 0
        if args.model:
            config["model_name"] = args.model
        if args.model_version:
            config["model_version"] = args.model_version
    if not args.dry_run and args.purpose == "development" and any(s.get("split") == "holdout" for s in sources):
        raise ValueError("Holdout samples require --purpose holdout; do not tune against holdout answers")
    frozen = freeze_metadata(sources, rules, knowledge_hash, limits, config, args.manifest, args.dpi)
    if args.freeze_record:
        if args.freeze_record.exists():
            previous = json.loads(args.freeze_record.read_text(encoding="utf-8"))
            if previous != frozen:
                raise ValueError("Frozen configuration mismatch; use the same sources, versions, fields and budgets")
        else:
            args.freeze_record.parent.mkdir(parents=True, exist_ok=True)
            args.freeze_record.write_text(json.dumps(frozen, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    args.out_dir.mkdir(parents=True, exist_ok=True)
    run_id = args.run_id or uuid4().hex
    if ((args.out_dir / "run-record.json").exists() or (args.out_dir / "summary.json").exists()
            or any((args.out_dir / f"{source['id']}.json").exists() for source in sources)):
        raise ValueError("Output exists; choose a new run directory")
    record = {"run_id": run_id, "purpose": args.purpose, "repeat_index": args.repeat_index,
              "frozen_configuration": frozen, "dry_run": args.dry_run}
    (args.out_dir / "run-record.json").write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    summary = []
    for source in sources:
        target = args.out_dir / f"{source['id']}.json"
        if target.exists():
            raise ValueError(f"Output exists; choose a new run directory: {target}")
        try:
            result = await run_sample(source, rules, knowledge_hash, limits, model_config=config,
                                      dpi=args.dpi, dry_run=args.dry_run)
        except Exception as exc:
            # 失败样本保留在本次运行中，并计入成功率分母。
            result = {"schema_version": 1, "document_id": source["id"], "sample_id": source["id"],
                      "status": "FAILED", "fields": [], "details": [], "pages": None,
                      "file_hash": source.get("expected_hash"), "error": f"{type(exc).__name__}: {exc}",
                      "run_metadata": {"duration_seconds": None, "accuracy_status": "UNVERIFIED"}}
        result["run_metadata"].update({"run_id": run_id,
                                       "purpose": args.purpose, "repeat_index": args.repeat_index,
                                       "frozen_configuration": frozen,
                                       "layout": source.get("layout"), "mine": source.get("mine")})
        target.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        row = {"id": source["id"], "split": source.get("split"), "mine": source.get("mine"),
               "status": result["status"], "fields": len(result["fields"]),
               "pages": result["pages"], "file_hash": result["file_hash"], "output": str(target),
               "duration_seconds": result["run_metadata"]["duration_seconds"]}
        summary.append(row)
        print(json.dumps(row, ensure_ascii=False), flush=True)
    (args.out_dir / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    return 0 if all(r["status"] in {"DRY_RUN", "COMPLETED", "PARTIAL"} for r in summary) else 2


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
