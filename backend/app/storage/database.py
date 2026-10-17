"""
数据库连接管理
"""

import logging
from collections.abc import AsyncGenerator

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.config import settings

logger = logging.getLogger(__name__)

# 创建异步引擎
engine = create_async_engine(
    settings.database.url,
    pool_size=settings.database.pool_size,
    max_overflow=settings.database.max_overflow,
    echo=settings.app.debug,
)

# 创建会话工厂
AsyncSessionLocal = async_sessionmaker(
    engine,
    class_=AsyncSession,
    expire_on_commit=False,
)


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """
    获取数据库会话（依赖注入）

    使用示例：
        @app.get("/items")
        async def get_items(db: AsyncSession = Depends(get_db)):
            ...
    """
    async with AsyncSessionLocal() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()


# 已有表需要补充的列。create_all 只建新表，不会改已有表结构，
# 这里显式补齐，保证升级时不需要手工执行 SQL。
COLUMN_MIGRATIONS: list[tuple[str, str, str]] = [
    # (表名, 列名, 列定义)
    ("content_build_jobs", "source_pages", "JSON"),
    ("extracted_fields", "data_type", "VARCHAR(20)"),
    ("extracted_fields", "unit", "VARCHAR(50)"),
    ("extracted_fields", "required", "BOOLEAN DEFAULT FALSE"),
    ("extracted_fields", "instances", "JSON"),
    ("extracted_fields", "rule_id", "UUID"),
    ("extracted_fields", "rule_version", "INTEGER"),
    ("extracted_fields", "validation_status", "VARCHAR(30)"),
    ("extracted_fields", "extraction_detail", "JSON"),
    ("extracted_fields", "quality_inputs", "JSON"),
    ("parse_runs", "checklist_type", "VARCHAR(100)"),
    ("parse_runs", "extraction_profile", "JSON"),
    ("parse_runs", "quality_status", "VARCHAR(30)"),
    ("parse_runs", "artifact_id", "UUID"),
    ("parse_runs", "parent_parse_run_id", "UUID"),
    ("parse_runs", "requested_field_keys", "JSON"),
    ("parse_runs", "run_mode", "VARCHAR(30)"),
    ("extraction_rules", "dimension_schema", "JSON DEFAULT '{}'"),
    ("extraction_rules", "value_kind", "VARCHAR(30) DEFAULT 'unknown'"),
    ("document_artifacts", "quality", "JSON"),
]


async def _apply_column_migrations(conn) -> None:
    """补齐已有表缺失的列（幂等）"""
    for table, column, definition in COLUMN_MIGRATIONS:
        exists = await conn.execute(
            text(
                "SELECT 1 FROM information_schema.columns "
                "WHERE table_name = :table AND column_name = :column"
            ),
            {"table": table, "column": column},
        )
        if exists.first():
            continue
        logger.info("添加列 %s.%s", table, column)
        await conn.execute(text(f'ALTER TABLE "{table}" ADD COLUMN "{column}" {definition}'))
    await conn.execute(text('ALTER TABLE "extracted_fields" ALTER COLUMN "field_value" DROP NOT NULL'))
    await conn.execute(text('ALTER TABLE "extracted_fields" ALTER COLUMN "confidence" DROP NOT NULL'))


async def init_db():
    """初始化数据库（建表 + 补列）"""
    from app.models import attachments as attachment_models  # noqa: F401
    from app.models import batches as batch_models  # noqa: F401
    from app.models import database as database_models  # noqa: F401  确保模型已注册
    from app.models import document_context as document_context_models  # noqa: F401
    from app.models import integrations as integration_models  # noqa: F401
    from app.models import releases as release_models  # noqa: F401
    from app.models import reviews as reviews_models  # noqa: F401
    from app.models import rules as rules_models  # noqa: F401
    from app.models import source_revisions as source_revision_models  # noqa: F401
    from app.models import shared_execution as shared_execution_models  # noqa: F401

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await _apply_column_migrations(conn)
        # 即使调用方直接使用 SQL，审计事实也只能追加。
        # 新决策必须创建修订；任何接口都不能覆盖自动提取结果。
        if conn.dialect.name == "postgresql":
            await conn.execute(text("""
                CREATE OR REPLACE FUNCTION reject_field_review_mutation() RETURNS trigger
                LANGUAGE plpgsql AS $$ BEGIN
                  RAISE EXCEPTION 'field_review_revisions is append-only';
                END; $$
            """))
            from app.models.releases import APPEND_ONLY_DDL
            for ddl in APPEND_ONLY_DDL:
                await conn.execute(text(ddl))
            from app.models.attachments import APPEND_ONLY_DDL as ATTACHMENT_HISTORY_DDL
            for ddl in ATTACHMENT_HISTORY_DDL:
                await conn.execute(text(ddl))
            for table in ("source_revisions", "document_contexts", "document_context_audits", "document_relation_audits"):
                await conn.execute(text(f"""
                    DO $$ BEGIN
                      IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname = '{table}_append_only') THEN
                        CREATE TRIGGER {table}_append_only BEFORE UPDATE OR DELETE ON {table}
                        FOR EACH ROW EXECUTE FUNCTION reject_rule_release_mutation();
                      END IF;
                    END; $$
                """))
            await conn.execute(text("""
                DO $$ BEGIN
                  IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname = 'field_review_append_only') THEN
                    CREATE TRIGGER field_review_append_only BEFORE UPDATE OR DELETE
                    ON field_review_revisions FOR EACH ROW EXECUTE FUNCTION reject_field_review_mutation();
                  END IF;
                END; $$
            """))
            # 已定版的 snapshot 不能再改。摘要写到旁表，列表查询就不用把十几 MB 的快照拉回来。
            await conn.execute(text("""
                INSERT INTO attachment_version_summaries (version_id, summary)
                SELECT v.id, json_build_object(
                    'stats', v.snapshot->'stats',
                    'duration_seconds', v.snapshot->'duration_seconds',
                    'cumulative_duration_seconds', v.snapshot->'cumulative_duration_seconds',
                    'content_revision_id', v.snapshot->'content_revision_id',
                    'invalidation_watermark', v.snapshot->'invalidation_watermark'
                )
                FROM attachment_versions v
                WHERE v.snapshot IS NOT NULL
                  AND NOT EXISTS (
                    SELECT 1 FROM attachment_version_summaries s WHERE s.version_id = v.id
                  )
            """))
            await conn.execute(text("""
                UPDATE extracted_fields
                SET quality_inputs = json_strip_nulls(json_build_object(
                    'contract_version', extraction_detail->'contract_version',
                    'semantic_state', extraction_detail->'semantic_state',
                    'coverage_complete', extraction_detail->'coverage_complete',
                    'confidence', extraction_detail->'confidence',
                    'coverage', extraction_detail->'coverage',
                    'failures', extraction_detail->'failures',
                    'value_groups', extraction_detail->'value_groups',
                    'instances', extraction_detail->'instances',
                    'field_completion', CASE WHEN extraction_detail->'field_completion' IS NULL THEN NULL
                        ELSE json_build_object(
                          'semantic_state', extraction_detail->'field_completion'->'semantic_state',
                          'coverage_complete', extraction_detail->'field_completion'->'coverage_complete') END,
                    'completion', CASE WHEN extraction_detail->'completion' IS NULL THEN NULL
                        ELSE json_build_object(
                          'semantic_state', extraction_detail->'completion'->'semantic_state',
                          'coverage_complete', extraction_detail->'completion'->'coverage_complete') END
                ))
                WHERE quality_inputs IS NULL AND extraction_detail IS NOT NULL
            """))

    logger.info("数据库初始化完成")


async def close_db():
    """关闭数据库连接"""
    await engine.dispose()


async def seed_default_rules() -> dict:
    """启动时确保种子规则存在"""
    from app.rules.seeding import seed_rules

    async with AsyncSessionLocal() as session:
        try:
            return await seed_rules(session)
        except Exception as exc:
            logger.error("写入种子规则失败: %s", exc, exc_info=True)
            await session.rollback()
            return {"error": str(exc)}


from app.models.database import Base  # noqa: E402  放在末尾避免循环导入
