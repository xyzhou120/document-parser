"""周期恢复任务的进程间互斥；连接退出即释放锁，不依赖固定 TTL。"""

from contextlib import asynccontextmanager

from sqlalchemy import text


@asynccontextmanager
async def control_task_lock(engine, lock_key):
    async with engine.connect() as connection:
        acquired = await connection.scalar(text("SELECT pg_try_advisory_lock(:key)"), {"key": lock_key})
        await connection.commit()
        try:
            yield bool(acquired)
        finally:
            if acquired:
                await connection.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": lock_key})
                await connection.commit()
