#!/usr/bin/env python3
"""Offline evaluation of the checklist agent on the reference reports (plan C0/C9, 2026-10-10).

The run follows the production path: PDF parse, region OCR for scanned pages through
``ParseService._ocr_scanned_pages``, then ``ChecklistAgent.extract_all`` with page and region
renders produced locally at the configured DPI.  Nothing is written to the database.

OCR answers are cached per page under ``<out>/ocr-cache`` (only non-empty answers are cached), so a
rerun costs model calls only.  ``--no-llm`` replaces the model with an empty answerer: it exercises
OCR, retrieval, prompts and budgets without spending tokens.

Usage, from the backend directory:
    python3 scripts/eval_checklist.py --docs D A C E --out ../outputs/eval/run1
    python3 scripts/eval_checklist.py --docs D --no-llm --out /tmp/eval_dry
Scoring uses scripts/eval/truth_checklist_2026-10-10.json; the rule snapshot is
scripts/eval/rules_checklist_2026-10-10.json.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from decimal import Decimal, InvalidOperation
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

BACKEND = Path(__file__).resolve().parents[1]
REPO = BACKEND.parent
sys.path.insert(0, str(BACKEND))

EVAL_DIR = BACKEND / "scripts" / "eval"
DOCS_DIR = REPO / "docs" / "附件下载0930_按煤矿整理" / "乔子梁煤业"


def log(message: str) -> None:
    print(message, file=sys.stderr, flush=True)


# --- scoring -------------------------------------------------------------------------

def to_number(text) -> Decimal | None:
    try:
        return Decimal(str(text).replace(",", "").strip())
    except (InvalidOperation, ValueError):
        return None


def value_matches(expected: str, observed, mode: str = "number") -> bool:
    if observed is None:
        return False
    if mode == "contains":
        return expected.replace(" ", "") in str(observed).replace(" ", "")
    a, b = to_number(expected), to_number(observed)
    if a is not None and b is not None:
        return a == b
    return str(observed).strip() == expected.strip()


def score(truth: list[dict], observed_by_doc: dict) -> dict:
    """Recall over truth values, precision over accepted values of labelled fields.

    ``observed_by_doc[doc][field_key]`` is the list of accepted instances (dicts with ``value``).
    Fields without a truth entry are not scored: their values are reported as unlabelled.
    """
    rows = []
    expected_total = expected_hit = 0
    labelled = labelled_true = 0
    labelled_keys = set()
    for item in truth:
        doc, key = item["doc"], item["field_key"]
        labelled_keys.add((doc, key))
        instances = observed_by_doc.get(doc, {}).get(key, [])
        observed = [i.get("value") for i in instances]
        mode = item.get("match", "number")
        if item.get("values"):
            hits = [v for v in item["values"] if any(value_matches(v, o, mode) for o in observed)]
            expected_total += len(item["values"])
            expected_hit += len(hits)
            rows.append({"doc": doc, "field_key": key, "expected": item["values"], "hit": hits,
                         "observed": observed, "ok": len(hits) == len(item["values"])})
        else:
            need = int(item.get("min_count", 1))
            expected_total += 1
            ok = len(observed) >= need
            expected_hit += 1 if ok else 0
            rows.append({"doc": doc, "field_key": key, "expected": f">= {need} instance(s)",
                         "hit": [], "observed": observed, "ok": ok})
    for doc, fields in observed_by_doc.items():
        for key, instances in fields.items():
            if (doc, key) not in labelled_keys:
                continue
            truth_values = [t for t in truth if t["doc"] == doc and t["field_key"] == key and t.get("values")]
            if not truth_values:
                continue
            allowed = truth_values[0]["values"]
            mode = truth_values[0].get("match", "number")
            for instance in instances:
                labelled += 1
                if any(value_matches(v, instance.get("value"), mode) for v in allowed):
                    labelled_true += 1
    unlabelled = sum(
        len(instances)
        for doc, fields in observed_by_doc.items()
        for key, instances in fields.items()
        if (doc, key) not in labelled_keys
    )
    return {
        "recall": round(expected_hit / expected_total, 4) if expected_total else None,
        "expected_total": expected_total,
        "expected_hit": expected_hit,
        "precision_labelled": round(labelled_true / labelled, 4) if labelled else None,
        "labelled_accepted": labelled,
        "labelled_true": labelled_true,
        "false_positive": labelled - labelled_true,
        "unlabelled_accepted": unlabelled,
        "rows": rows,
    }


# --- runtime pieces ---------------------------------------------------------------------

class NoLLMClient:
    """Stands in for the vision model: answers every request with an empty result list."""

    def __init__(self):
        self.calls = []
        self.chat = self
        self.completions = self

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        content = json.dumps({"results": []}, ensure_ascii=False)
        usage = SimpleNamespace(prompt_tokens=0, completion_tokens=0, total_tokens=0)
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=content),
                                                        finish_reason="stop")], usage=usage)


def block_to_json(block) -> dict:
    bbox = None
    if block.locator is not None:
        box = block.locator.bbox
        bbox = [box.x0, box.y0, box.x1, box.y1]
    return {"text": block.text, "label": block.label, "bbox": bbox}


def block_from_json(item: dict, page_number: int):
    from app.models.contracts import Box, Locator, TextBlock

    bbox = item.get("bbox")
    locator = None
    if bbox:
        locator = Locator(coordinate_system="PIXEL", page_number=page_number,
                          bbox=Box(x0=bbox[0], y0=bbox[1], x1=bbox[2], y1=bbox[3]))
    return TextBlock(text=item["text"], label=item.get("label"), confidence=None,
                     location_status="ocr_region" if locator else "unavailable", locator=locator)


class RegionCache:
    """Caches non-empty region-OCR answers per page; empty answers are retried on rerun."""

    def __init__(self, directory: Path, tag: str, original):
        self.directory = directory
        self.tag = tag
        self.original = original
        self.hits = 0
        self.misses = 0
        directory.mkdir(parents=True, exist_ok=True)

    async def __call__(self, image, page_number, width, height):
        path = self.directory / f"{self.tag}_p{page_number:03d}.json"
        if path.exists():
            self.hits += 1
            return [block_from_json(item, page_number) for item in json.loads(path.read_text(encoding="utf-8"))]
        self.misses += 1
        blocks = await self.original(image, page_number, width, height)
        if blocks:
            path.write_text(json.dumps([block_to_json(b) for b in blocks], ensure_ascii=False), encoding="utf-8")
        return blocks


def image_provider_for(pdf_path: Path, render_dpi: int, region_dpi: int):
    import pymupdf

    document = pymupdf.open(str(pdf_path))

    async def provider(page_number: int, bbox):
        page = document[page_number - 1]
        clip = pymupdf.Rect(*bbox) if bbox else None
        dpi = region_dpi if bbox else render_dpi
        return page.get_pixmap(dpi=dpi, clip=clip, alpha=False).tobytes("png")

    return provider, document


# --- one document -------------------------------------------------------------------------

def summarize_detail(detail: dict) -> dict:
    instances = []
    for item in detail.get("instances", []):
        instances.append({
            "value": item.get("value"),
            "unit": item.get("unit"),
            "page": item.get("page_num"),
            "bbox": item.get("bbox"),
            "strength": item.get("candidate_strength"),
            "image_checked": item.get("image_checked"),
            "hints": item.get("candidate_hints"),
            "issues": [q.get("code") for q in item.get("quality_issues", [])],
            "dims": {k: (v.get("value") if isinstance(v, dict) else v)
                     for k, v in (item.get("dimensions") or {}).items()},
            "quote": (item.get("quote") or "")[:200],
        })
    return {
        "field_key": detail.get("field_key"),
        "validation_status": detail.get("validation_status"),
        "incomplete_reasons": detail.get("incomplete_reasons"),
        "retrieval": detail.get("retrieval"),
        "instance_count": len(instances),
        "instances": instances,
    }


async def run_document(tag: str, args, truth: dict, rules_profile: dict, out_dir: Path) -> dict:
    """Run one document (or the ``--fields`` subset) and merge the result into ``<out>/<tag>.json``.

    Sandbox calls are short, so a long document is run field by field across several calls; each
    call adds its fields and its own token/call totals to the document record."""
    from app.config import settings
    from app.agents.checklist import ChecklistAgent
    from app.services.parse_service import ParseService, snapshot_rules

    info = truth["documents"][tag]
    pdf_path = DOCS_DIR / info["file"]
    started = time.time()
    service = ParseService()
    service._log = AsyncMock()
    cache = RegionCache(out_dir / "ocr-cache", tag, service.ocr_client.recognize_regions)
    service.ocr_client.recognize_regions = cache
    pages = service.pdf_parser.extract_pages(str(pdf_path))
    log(f"[{tag}] {info['label']}: {len(pages)} pages; OCR for scanned pages")
    ocr_blocks, ocr_failed = await service._ocr_scanned_pages(
        AsyncMock(), None, pages, pdf_path, True)
    ocr_pages = sorted(p["page_number"] for p in pages if p.get("ocr_status") == "ok")
    log(f"[{tag}] OCR ok {len(ocr_pages)} pages, failed {sorted(ocr_failed)}; "
        f"cache hits {cache.hits}, misses {cache.misses}")

    wanted = set(args.fields or [])
    profile = {**rules_profile, "rules": [r for r in rules_profile["rules"]
                                          if not wanted or r["field_key"] in wanted]}
    rules = snapshot_rules(profile)
    agent_config = settings.agent.model_dump()
    agent_config.update(args.agent_overrides)
    agent_config["agent_mode"] = args.mode
    vision = settings.models["vision_llm"]
    model_config = {"model_name": vision.model_name, "model_version": vision.model_version,
                    "base_url": vision.base_url, "timeout": vision.timeout,
                    "api_key": vision.api_key, "max_retries": 0}
    provider, document = image_provider_for(pdf_path, agent_config["render_dpi"],
                                            agent_config["region_render_dpi"])
    client = NoLLMClient() if args.no_llm else None
    agent = ChecklistAgent(settings_override=SimpleNamespace(**agent_config),
                           client=client, model_config=model_config)
    try:
        fields, details, runtime = await agent.extract_all(pages, rules, provider)
    finally:
        document.close()
    calls = [t for t in runtime.get("trace", []) if t.get("event") == "model_response"]
    run_record = {
        "fields_run": [r.field_key for r in rules],
        "run_status": runtime.get("status"),
        "token_usage": runtime.get("token_usage"),
        "rounds_used": runtime.get("rounds_used"),
        "failures": sorted(set(runtime.get("failures", []))),
        "calls": [{"field_key": c.get("field_key"), "call": c.get("call"),
                   "estimate": c.get("estimate"), "max_tokens": c.get("max_tokens"),
                   "image_count": c.get("image_count"), "calibration": c.get("calibration"),
                   "finish_reason": c.get("finish_reason"), "usage": c.get("usage")} for c in calls],
        "elapsed_seconds": round(time.time() - started, 1),
    }
    path = out_dir / f"{tag}.json"
    result = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {
        "doc": tag, "label": info["label"], "file": info["file"], "page_count": len(pages),
        "ocr": {"scanned_pages": ocr_pages, "failed_pages": sorted(ocr_failed)},
        "agent_config": {k: agent_config[k] for k in sorted(agent_config) if k in {
            "agent_mode", "thinking_enabled", "token_per_char", "min_output_tokens",
            "field_token_budget", "max_calls_per_field", "candidates_per_call",
            "max_candidates_per_field", "max_candidates_multi", "max_images_per_call",
            "max_image_tokens_per_call", "candidate_window_chars", "retrieval_image_policy",
            "render_dpi", "region_render_dpi"}},
        "runs": [], "fields": {},
    }
    result["runs"].append(run_record)
    for detail in details:
        result["fields"][detail.get("field_key")] = summarize_detail(detail)
    result["token_usage"] = {k: sum((r["token_usage"] or {}).get(k, 0) for r in result["runs"])
                             for k in ("prompt_tokens", "completion_tokens", "total_tokens")}
    result["calls_total"] = sum(len(r["calls"]) for r in result["runs"])
    result["run_status"] = "MERGED" if len(result["runs"]) > 1 else runtime.get("status")
    path.write_text(json.dumps(result, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    log(f"[{tag}] fields={run_record['fields_run']} status={run_record['run_status']} "
        f"tokens={(run_record['token_usage'] or {}).get('total_tokens')} calls={len(calls)} "
        f"elapsed={run_record['elapsed_seconds']}s failures={run_record['failures']}")
    return result


def parse_overrides(pairs: list[str]) -> dict:
    overrides = {}
    for pair in pairs:
        key, _, raw = pair.partition("=")
        try:
            overrides[key] = json.loads(raw)
        except json.JSONDecodeError:
            overrides[key] = raw
    return overrides


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--docs", nargs="+", default=["D", "A", "C", "E"])
    parser.add_argument("--mode", default="retrieval", choices=["retrieval", "legacy"])
    parser.add_argument("--out", required=True)
    parser.add_argument("--set", dest="sets", action="append", default=[],
                        help="agent setting override, e.g. --set thinking_enabled=false")
    parser.add_argument("--no-llm", action="store_true")
    parser.add_argument("--fields", nargs="+", default=None,
                        help="run only these field keys (results merge into the document record)")
    parser.add_argument("--score-only", action="store_true",
                        help="rescore the JSON files already in --out without running anything")
    args = parser.parse_args(argv)
    args.agent_overrides = parse_overrides(args.sets)
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    truth = json.loads((EVAL_DIR / "truth_checklist_2026-10-10.json").read_text(encoding="utf-8"))
    rules_profile = json.loads((EVAL_DIR / "rules_checklist_2026-10-10.json").read_text(encoding="utf-8"))

    if args.score_only:
        results = {tag: json.loads((out_dir / f"{tag}.json").read_text(encoding="utf-8")) for tag in args.docs}
    else:
        results = {}
        for tag in args.docs:
            results[tag] = asyncio.run(run_document(tag, args, truth, rules_profile, out_dir))

    observed = {tag: {key: f["instances"] for key, f in res["fields"].items()} for tag, res in results.items()}
    scored = score(truth["truth"], observed)
    summary = {
        "mode": args.mode,
        "overrides": args.agent_overrides,
        "no_llm": args.no_llm,
        "docs": {tag: {
            "label": res["label"],
            "status": res.get("run_status"),
            "total_tokens": (res["token_usage"] or {}).get("total_tokens"),
            "calls": res.get("calls_total"),
            "runs": len(res.get("runs", [])),
            "failures": sorted({x for r in res.get("runs", []) for x in r["failures"]}),
            "statuses": {key: f["validation_status"] for key, f in res["fields"].items()},
            "accepted": {key: f["instance_count"] for key, f in res["fields"].items()},
        } for tag, res in results.items()},
        "score": scored,
    }
    (out_dir / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=1, default=str),
                                          encoding="utf-8")
    print(json.dumps({"recall": scored["recall"], "precision_labelled": scored["precision_labelled"],
                      "false_positive": scored["false_positive"],
                      "expected_hit": f'{scored["expected_hit"]}/{scored["expected_total"]}',
                      "unlabelled_accepted": scored["unlabelled_accepted"]}, ensure_ascii=False))
    for row in scored["rows"]:
        print(f'{row["doc"]} {row["field_key"]:34s} ok={row["ok"]!s:5s} expected={row["expected"]} '
              f'hit={row["hit"]} observed={row["observed"]}')
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
