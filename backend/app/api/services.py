"""服务管理 API

真实探测各外部依赖的连通性，不返回写死的示例状态。
"""

import asyncio
import logging
import time

import httpx
from fastapi import APIRouter, Depends
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.storage import get_db
from app.storage.minio_client import minio_client

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/services", tags=["服务管理"])


async def _probe_http_client(timeout: float):
    # Loading platform certificates/proxy configuration can block on Windows.
    # Do it off the event loop so independent Redis/DB probes retain their own
    # timeout budget rather than expiring behind HTTP client construction.
    return await asyncio.to_thread(httpx.AsyncClient, timeout=timeout)


async def _check_database(db: AsyncSession) -> dict:
    started = time.perf_counter()
    try:
        await db.execute(text("SELECT 1"))
        latency = (time.perf_counter() - started) * 1000
        return {
            "name": "PostgreSQL",
            "key": "database",
            "status": "online",
            "latency_ms": round(latency, 1),
            "detail": f"{settings.database.host}:{settings.database.port}/{settings.database.name}",
        }
    except Exception as exc:
        logger.warning("数据库探测失败: %s", exc)
        return {
            "name": "PostgreSQL",
            "key": "database",
            "status": "offline",
            "latency_ms": None,
            "detail": str(exc)[:200],
        }


async def _check_redis() -> dict:
    started = time.perf_counter()
    try:
        def ping():
            import redis
            with redis.Redis(host=settings.redis.host, port=settings.redis.port,
                             db=settings.redis.db, password=settings.redis.password or None,
                             socket_timeout=5, socket_connect_timeout=5) as client:
                client.ping()
                # INFO may be denied by a least-privilege Redis account. PING
                # is the connectivity check; optional metadata must not turn
                # a reachable queue into a false offline result.
                try:
                    return client.info("server").get("redis_version", "未知")
                except redis.exceptions.ResponseError:
                    return "未知"
        version = await asyncio.to_thread(ping)
        latency = (time.perf_counter() - started) * 1000
        return {"name": "Redis", "key": "redis", "status": "online",
                "latency_ms": round(latency, 1),
                "detail": f"{settings.redis.host}:{settings.redis.port} · v{version}"}
    except Exception as exc:
        logger.warning("Redis 探测失败: %s", exc)
        return {
            "name": "Redis",
            "key": "redis",
            "status": "offline",
            "latency_ms": None,
            "detail": str(exc)[:200],
        }


async def _check_minio() -> dict:
    started = time.perf_counter()
    try:
        import asyncio

        exists = await asyncio.to_thread(minio_client.client.bucket_exists, settings.minio.bucket)
        latency = (time.perf_counter() - started) * 1000
        return {
            "name": "MinIO 对象存储",
            "key": "minio",
            "status": "online" if exists else "degraded",
            "latency_ms": round(latency, 1),
            "detail": f"{settings.minio.endpoint} · bucket {settings.minio.bucket}"
            + ("" if exists else "（bucket 不存在）"),
        }
    except Exception as exc:
        logger.warning("MinIO 探测失败: %s", exc)
        return {
            "name": "MinIO 对象存储",
            "key": "minio",
            "status": "offline",
            "latency_ms": None,
            "detail": str(exc)[:200],
        }


async def _check_ocr() -> dict:
    ocr_config = settings.models.get("ocr")
    if not ocr_config:
        return {
            "name": "OCR 识别服务",
            "key": "ocr",
            "status": "unconfigured",
            "latency_ms": None,
            "detail": "未配置 models.ocr",
        }

    from app.parsers.ocr_client import PaddleOCRClient

    base = PaddleOCRClient._normalize_base(ocr_config.base_url)
    started = time.perf_counter()
    try:
        async with await _probe_http_client(8) as client:
            response = await client.get(f"{base}/ping")
        latency = (time.perf_counter() - started) * 1000
        if response.status_code == 200:
            return {
                "name": "OCR 识别服务",
                "key": "ocr",
                "status": "online",
                "latency_ms": round(latency, 1),
                "detail": base,
            }
        return {
            "name": "OCR 识别服务",
            "key": "ocr",
            "status": "degraded",
            "latency_ms": round(latency, 1),
            "detail": f"{base} 返回 HTTP {response.status_code}",
        }
    except Exception as exc:
        logger.warning("OCR 探测失败: %s", exc)
        return {
            "name": "OCR 识别服务",
            "key": "ocr",
            "status": "offline",
            "latency_ms": None,
            "detail": f"{base} 不可达: {exc}"[:200],
        }


async def _check_text_llm() -> dict:
    llm_config = settings.models.get("text_llm")
    if not llm_config:
        return {
            "name": "文本大模型",
            "key": "text_llm",
            "status": "unconfigured",
            "latency_ms": None,
            "detail": "未配置 models.text_llm",
        }

    started = time.perf_counter()
    # 与 LLMStrategy 保持一致的归一化：缺少 /v1 时补上
    base = (llm_config.base_url or "").strip().rstrip("/")
    if base and not base.endswith("/v1"):
        base = f"{base}/v1"
    url = f"{base}/models"
    headers = {"Authorization": f"Bearer {llm_config.api_key or 'dummy'}"}
    try:
        async with await _probe_http_client(12) as client:
            response = await client.get(url, headers=headers)
        latency = (time.perf_counter() - started) * 1000
        available: list[str] = []
        if response.status_code == 200:
            try:
                available = [m.get("id") for m in response.json().get("data", [])]
            except Exception:
                available = []
        configured = llm_config.model_name
        model_ok = (not available) or (configured in available)
        return {
            "name": "文本大模型",
            "key": "text_llm",
            "status": "online" if response.status_code == 200 and model_ok else "degraded",
            "latency_ms": round(latency, 1),
            "detail": (
                f"{configured} · {llm_config.base_url}"
                if model_ok
                else f"配置模型 {configured} 不在服务模型列表中，可用：{', '.join(available[:5])}"
            ),
        }
    except Exception as exc:
        logger.warning("大模型探测失败: %s", exc)
        return {
            "name": "文本大模型",
            "key": "text_llm",
            "status": "offline",
            "latency_ms": None,
            "detail": f"{llm_config.base_url} 不可达: {exc}"[:200],
        }


async def _check_worker() -> dict:
    """检查 Celery Worker 是否在线（通过 Celery inspect ping）"""
    try:
        import asyncio

        from app.celery_app import celery_app

        started = time.perf_counter()
        replies = await asyncio.wait_for(
            asyncio.to_thread(celery_app.control.inspect(timeout=3).ping), timeout=5
        )
        online = bool(replies)
        return {
            "name": "Celery Worker",
            "key": "worker",
            "status": "online" if online else "offline",
            "latency_ms": round((time.perf_counter() - started) * 1000, 1),
            "detail": (
                "已发现 Worker 注册信息"
                if online
                else "未发现 Worker，解析任务会停留在等待状态，请启动 Worker"
            ),
        }
    except Exception as exc:
        return {
            "name": "Celery Worker",
            "key": "worker",
            "status": "unknown",
            "latency_ms": None,
            "detail": f"无法探测: {exc}"[:200],
        }


@router.get("", summary="服务状态总览")
async def list_services(db: AsyncSession = Depends(get_db)):
    """探测所有依赖服务的真实状态"""
    import asyncio

    results = await asyncio.gather(
        _check_database(db),
        _check_redis(),
        _check_minio(),
        _check_ocr(),
        _check_text_llm(),
        _check_worker(),
        return_exceptions=True,
    )

    services = []
    for item in results:
        if isinstance(item, Exception):
            services.append(
                {
                    "name": "未知服务",
                    "key": "unknown",
                    "status": "unknown",
                    "latency_ms": None,
                    "detail": str(item)[:200],
                }
            )
        else:
            services.append(item)

    online = sum(1 for s in services if s["status"] == "online")
    return {
        "services": services,
        "online_count": online,
        "total_count": len(services),
        "healthy": online == len(services),
    }
