"""
Celery应用配置
"""

from celery import Celery

from app.config import settings

# 创建Celery应用
celery_app = Celery(
    "document-parser",
    broker=settings.celery.broker_url,
    backend=settings.celery.result_backend,
)

# 配置
beat_schedule = {}
if settings.attachment_import.enabled:
    beat_schedule["attachment-import-recovery"] = {"task": "attachment_import_recovery", "schedule": 30.0,
                                                  "options": {"queue": "attachment-control", "expires": 60}}
    beat_schedule["attachment-object-cleanup"] = {"task": "attachment_object_cleanup", "schedule": 86400.0,
                                                "options": {"queue": "celery"}}
if settings.app.reliable_enabled:
    beat_schedule["reliable-execution-recovery"] = {"task": "reliable_execution_dispatch", "schedule": 30.0,
                                                  "options": {"queue": "reliable-control", "expires": 60}}
if settings.app.assistant_enabled:
    beat_schedule["assistant-message-recovery"] = {"task": "assistant_outbox_dispatch", "schedule": 20.0,
                                                 "options": {"queue": "assistant", "expires": 60}}
celery_app.conf.update(
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    timezone=settings.celery.timezone,
    enable_utc=settings.celery.enable_utc,
    task_track_started=True,
    task_time_limit=3600,  # 1小时超时
    worker_prefetch_multiplier=1,  # 每次只取1个任务
    # 网络/睡眠恢复后持续重连；SQL 账本与 fence 负责重复投递去重。
    broker_connection_retry_on_startup=True,
    broker_connection_retry=True,
    broker_connection_max_retries=None,
    broker_transport_options={
        "socket_keepalive": True,
        "socket_connect_timeout": 5,
        "socket_timeout": 10,
        "health_check_interval": 30,
        "retry_on_timeout": True,
    },
    task_default_queue=settings.celery.task_default_queue,
    # 新执行唤醒任务与现有 parse_document 任务隔离。
    task_routes={"reliable_execution_wakeup": {"queue": "reliable-execution"},
                 "shared_execution_materialize": {"queue": "reliable-execution"},
                 "attachment_import_preview": {"queue": "celery"},
                 "attachment_download": {"queue": "celery"},
                 "attachment_object_cleanup": {"queue": "celery"},
                 "attachment_import_recovery": {"queue": "attachment-control"},
                 "reliable_content_build": {"queue": "reliable-execution"},
                 "reliable_execution_dispatch": {"queue": "reliable-control"},
                 "assistant_message_execute": {"queue": "assistant"},
                 "assistant_outbox_dispatch": {"queue": "assistant"},
                 "assistant_card_build": {"queue": "assistant"},
                 "content_index_build": {"queue": "assistant"}},
    beat_schedule=beat_schedule,
)

# 自动发现任务
celery_app.autodiscover_tasks(["app"])
