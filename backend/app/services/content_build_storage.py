"""内容构建的页/区域增量存储，旧检查点在下一次受栅栏保护的保存时转换。"""

import hashlib
import json
from copy import deepcopy

from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert

from app.models.content import ContentBuildEntry

CONTENT_CHECKPOINT_V2 = "content-checkpoint-v2"
REGION_STATUS_KEYS = {"region_id", "page_number", "state", "source", "cache_key", "attempts",
                      "image_hash", "error", "bbox", "coordinate_system", "location_precision"}


class ContentCheckpoint(dict):
    """指纹仅保留在内存，不进入 JSON，也不跨 worker 共享可变状态。"""

    def __init__(self, value=(), *, fingerprints=None):
        super().__init__(value)
        self.fingerprints = fingerprints or {}


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                     separators=(",", ":")).encode()).hexdigest()


def compact_manifest(manifest):
    return {**manifest, "pages": [
        {**page, "regions": [{k: v for k, v in region.items() if k in REGION_STATUS_KEYS}
                             for region in page.get("regions", [])]}
        for page in manifest.get("pages", [])]}


async def load_checkpoint(db, job):
    checkpoint = ContentCheckpoint(deepcopy(job.checkpoint or {}))
    # 源输入独立存一次；过渡期旧输入仍从 checkpoint 读取。
    # defer 避免心跳/领取行锁时下载正文，显式异步读取只发生在恢复入口。
    source = await db.scalar(select(type(job).source_pages).where(type(job).build_id == job.build_id))
    checkpoint["source_pages"] = deepcopy(source if source is not None else checkpoint.get("source_pages"))
    checkpoint["source_pages_stored"] = source is not None
    checkpoint.setdefault("regions", {})
    checkpoint.setdefault("prefetched_ocr", {})
    if checkpoint.get("schema_version") == CONTENT_CHECKPOINT_V2:
        rows = (await db.scalars(select(ContentBuildEntry).where(
            ContentBuildEntry.build_id == job.build_id))).all()
        for row in rows:
            key = (row.kind, row.entry_key)
            checkpoint.fingerprints[key] = fingerprint(row.payload)
            checkpoint["regions" if row.kind == "region" else "prefetched_ocr"][row.entry_key] = deepcopy(row.payload)
    checkpoint["schema_version"] = CONTENT_CHECKPOINT_V2
    return checkpoint


def prepare_storage(build_id, checkpoint, jsonable):
    """在取行锁前计算差量与紧凑进度；不进行数据库 I/O。"""
    entries = {}
    for kind, name in (("region", "regions"), ("prefetch", "prefetched_ocr")):
        for key, value in checkpoint.get(name, {}).items():
            entries[(kind, key)] = jsonable(value)
    previous = getattr(checkpoint, "fingerprints", {})
    fingerprints = {key: fingerprint(value) for key, value in entries.items()}
    changed = [{"build_id": build_id, "kind": key[0], "entry_key": key[1], "payload": value}
               for key, value in entries.items() if previous.get(key) != fingerprints[key]]
    compact = {key: value for key, value in checkpoint.items()
               if key not in {"source_pages", "regions", "prefetched_ocr", "coverage_manifest", "pages"}}
    compact["schema_version"] = CONTENT_CHECKPOINT_V2
    compact["source_pages_stored"] = checkpoint.get("source_pages") is not None or bool(checkpoint.get("source_pages_stored"))
    compact["pages"] = {key: {k: v for k, v in value.items() if k != "native_page"}
                        for key, value in checkpoint.get("pages", {}).items()}
    compact["regions"] = {key: {k: v for k, v in value.items() if k in REGION_STATUS_KEYS}
                          for key, value in checkpoint.get("regions", {}).items()}
    compact["prefetched_ocr"] = {key: {"stored": True} for key in checkpoint.get("prefetched_ocr", {})}
    if checkpoint.get("coverage_manifest"):
        compact["coverage_manifest"] = compact_manifest(checkpoint["coverage_manifest"])
    source = jsonable(checkpoint["source_pages"]) if not checkpoint.get("source_pages_stored") and checkpoint.get("source_pages") is not None else None
    return jsonable(compact), fingerprints, changed, list(previous.keys() - entries.keys()), source


async def store_checkpoint(db, job, prepared):
    """调用者已验证 fence/lease；差量结果与紧凑进度原子提交。"""
    compact, fingerprints, changed, removed, source = prepared
    for offset in range(0, len(changed), 50):
        stmt = insert(ContentBuildEntry).values(changed[offset:offset + 50])
        await db.execute(stmt.on_conflict_do_update(
            index_elements=["build_id", "kind", "entry_key"], set_={"payload": stmt.excluded.payload}))
    for kind, key in removed:
        await db.execute(delete(ContentBuildEntry).where(ContentBuildEntry.build_id == job.build_id,
            ContentBuildEntry.kind == kind, ContentBuildEntry.entry_key == key))
    if source is not None:
        job.source_pages = source
    return compact, fingerprints, len(changed)
