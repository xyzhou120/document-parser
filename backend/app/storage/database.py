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
    ("extracted_fields", "data_type", "VARCHAR(20)"),
    ("extracted_fields", "unit", "VARCHAR(50)"),
    ("extracted_fields", "required", "BOOLEAN DEFAULT FALSE"),
    ("extracted_fields", "instances", "JSON"),
    ("extracted_fields", "rule_id", "UUID"),
    ("extracted_fields", "rule_version", "INTEGER"),
    ("extracted_fields", "validation_status", "VARCHAR(30)"),
    ("extracted_fields", "extraction_detail", "JSON"),
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
    from app.models import batches as batch_models  # noqa: F401
    from app.models import database as database_models  # noqa: F401  确保模型已注册
    from app.models import document_context as document_context_models  # noqa: F401
    from app.models import integrations as integration_models  # noqa: F401
    from app.models import releases as release_models  # noqa: F401
    from app.models import reviews as reviews_models  # noqa: F401
    from app.models import rules as rules_models  # noqa: F401
    from app.models import source_revisions as source_revision_models  # noqa: F401

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
