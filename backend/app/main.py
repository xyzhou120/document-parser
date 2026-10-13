"""
FastAPI应用主入口
"""

import logging

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api import (
    artifacts_router,
    assistant_router,
    batches_router,
    documents_router,
    integrations_router,
    observability_router,
    parse_router,
    releases_router,
    review_workspace_router,
    reviews_router,
    rules_router,
    services_router,
    stats_router,
)
from app.api.assistant_auxiliary import router as assistant_auxiliary_router
from app.api.reliable_execution import router as reliable_execution_router
from app.api.reliable_execution import start_router as reliable_start_router
from app.config import settings
from app.security import AccessControlMiddleware
from app.storage import close_db, init_db, seed_default_rules

# 配置日志
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)

logger = logging.getLogger(__name__)

# 创建FastAPI应用
app = FastAPI(
    title="智能文档解析平台",
    description="煤矿文档解析与关键信息提取平台",
    version="1.0.0",
    docs_url="/docs",
    redoc_url="/redoc",
)

# CORS中间件
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # 生产环境需要限制
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.add_middleware(AccessControlMiddleware)


@app.on_event("startup")
async def startup_event():
    """应用启动事件：建表、补列、写入种子规则"""
    logger.info("Starting document-parser backend...")
    await init_db()
    logger.info("Database initialized")

    result = await seed_default_rules()
    logger.info("Rule seeding: %s", result)


@app.on_event("shutdown")
async def shutdown_event():
    """应用关闭事件"""
    logger.info("Shutting down document-parser backend...")
    await close_db()


@app.get("/health", tags=["健康检查"])
@app.get("/api/health", tags=["健康检查"])
async def health_check():
    """健康检查接口"""
    return {"status": "ok", "service": "document-parser"}


# 注册路由
app.include_router(parse_router)
app.include_router(artifacts_router)
app.include_router(batches_router)
app.include_router(documents_router)
app.include_router(integrations_router)
app.include_router(observability_router)
app.include_router(releases_router)
app.include_router(rules_router)
app.include_router(stats_router)
app.include_router(services_router)
app.include_router(reviews_router)
app.include_router(review_workspace_router)
app.include_router(reliable_execution_router)
app.include_router(reliable_start_router)
if settings.app.assistant_enabled:
    app.include_router(assistant_router)
    app.include_router(assistant_auxiliary_router)

if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "app.main:app",
        host="0.0.0.0",
        port=8000,
        reload=True,
        log_level="info",
    )
