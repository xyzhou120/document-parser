"""只读运行指标；未知的成本和 GPU 数据绝不伪造。"""

import math
from collections import Counter
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.database import ParseRun
from app.storage import get_db

router = APIRouter(prefix="/api/v1/observability", tags=["运行观测"])


def summarize_runs(runs: list) -> dict:
    states = Counter(run.status for run in runs)
    durations, waits, tokens, partial = [], [], [], 0
    for run in runs:
        value = run.duration_seconds
        if isinstance(value, (int, float)) and math.isfinite(value) and value >= 0:
            durations.append(float(value))
        if run.started_at and run.created_at:
            wait = (run.started_at - run.created_at).total_seconds()
            if wait >= 0:
                waits.append(wait)
        runtime = (run.extraction_profile or {}).get("agent_runtime") or {}
        partial += runtime.get("status") in {"PARTIAL", "UNRESOLVED"}
        count = (runtime.get("token_usage") or {}).get("total_tokens")
        if isinstance(count, int) and not isinstance(count, bool) and count >= 0:
            tokens.append(count)

    def distribution(values):
        ordered = sorted(values)
        def percentile(q):
            if not ordered:
                return None
            index = (len(ordered) - 1) * q
            lower, upper = math.floor(index), math.ceil(index)
            return round(ordered[lower] + (ordered[upper] - ordered[lower]) * (index - lower), 3)
        return {"measured_runs": len(ordered), "p50_seconds": percentile(.5),
                "p95_seconds": percentile(.95),
                "mean_seconds": round(sum(ordered) / len(ordered), 3) if ordered else None}

    return {"sample_runs": len(runs), "states": dict(states), "duration": distribution(durations),
            "queue_wait": distribution(waits), "partial_or_unresolved": partial,
            "token_usage": {"measured_runs": len(tokens), "total_tokens": sum(tokens) if tokens else None},
            "gpu_usage": None, "actual_cost": None,
            "warning": "运行观测不是并发容量或真实准确率验收；未记录费用/GPU保持未知。"}


@router.get("/runs")
async def run_metrics(days: int = Query(7, ge=1, le=90),
                      limit: int = Query(2000, ge=1, le=10000),
                      db: AsyncSession = Depends(get_db)):
    rows = (await db.execute(select(ParseRun).where(ParseRun.created_at >= datetime.utcnow() - timedelta(days=days))
        .order_by(ParseRun.created_at.desc()).limit(limit + 1))).scalars().all()
    return {"days": days, "limit": limit, "truncated": len(rows) > limit,
            **summarize_runs(list(rows[:limit]))}
