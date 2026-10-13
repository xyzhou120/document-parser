"""显式选择数据库；离线生成 SQL 不需要凭据。"""

import asyncio
import os

from alembic import context
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from migrations.metadata import load_metadata

target_metadata = load_metadata()


def configure(connection):
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        compare_type=True,
        compare_server_default=True,
    )
    with context.begin_transaction():
        context.run_migrations()


async def online():
    url = os.environ.get("PLATFORM_MIGRATION_DATABASE_URL")
    if not url:
        raise RuntimeError("Set PLATFORM_MIGRATION_DATABASE_URL to an explicit target")
    if not url.startswith("postgresql+asyncpg://"):
        raise RuntimeError("Migration target must use postgresql+asyncpg")
    engine = create_async_engine(url, poolclass=NullPool, echo=False)
    try:
        async with engine.connect() as connection:
            await connection.run_sync(configure)
    finally:
        await engine.dispose()


if context.is_offline_mode():
    context.configure(
        dialect_name="postgresql",
        target_metadata=target_metadata,
        literal_binds=True,
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()
else:
    asyncio.run(online())
