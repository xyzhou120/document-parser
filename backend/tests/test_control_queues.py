"""验证控制队列隔离与升级前恢复消息的消费边界。"""

from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, Mock

from app.celery_app import celery_app
from app.tasks import attachment_import_recovery_task


def test_control_tasks_route_to_independent_consumers():
    router = celery_app.amqp.Router()
    assert router.route({}, "attachment_import_recovery")["queue"].name == "attachment-control"
    assert router.route({}, "reliable_execution_dispatch")["queue"].name == "reliable-control"
    schedule = celery_app.conf.beat_schedule
    if "attachment-import-recovery" in schedule:
        assert schedule["attachment-import-recovery"]["options"] == {
            "queue": "attachment-control", "expires": 60,
        }


def test_legacy_import_recovery_does_not_scan_database(monkeypatch):
    engine = Mock(side_effect=AssertionError("旧恢复消息不能访问数据库"))
    monkeypatch.setattr("app.tasks.create_async_engine", engine)
    attachment_import_recovery_task.push_request(delivery_info={"routing_key": "reliable-control"})
    try:
        assert attachment_import_recovery_task.run() == {"status": "obsolete_queue"}
    finally:
        attachment_import_recovery_task.pop_request()
    engine.assert_not_called()


def test_new_import_queue_still_recovers_persisted_work(monkeypatch):
    engine = Mock(dispose=AsyncMock())
    sessions = Mock()
    recover = AsyncMock(return_value={"download_wakeups": 2})

    @asynccontextmanager
    async def lock(*args):
        yield True

    monkeypatch.setattr("app.tasks.create_async_engine", Mock(return_value=engine))
    monkeypatch.setattr("app.tasks.async_sessionmaker", Mock(return_value=sessions))
    monkeypatch.setattr("app.services.control_task_lock.control_task_lock", lock)
    monkeypatch.setattr("app.services.attachment_runtime.recover_imports", recover)
    attachment_import_recovery_task.push_request(delivery_info={"routing_key": "attachment-control"})
    try:
        assert attachment_import_recovery_task.run() == {"download_wakeups": 2}
    finally:
        attachment_import_recovery_task.pop_request()
    recover.assert_awaited_once()
    assert recover.await_args.args[0] is sessions
    engine.dispose.assert_awaited_once()
