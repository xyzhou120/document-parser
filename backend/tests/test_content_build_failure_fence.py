"""At-least-once content worker failure authority, independent of broker/DB."""

from datetime import datetime, timedelta
from types import SimpleNamespace

from app.services.content_build_service import ContentBuildError
from app.tasks import content_build_failure_allowed


def job(*, fence=1, owner="celery:a", until=None):
    return SimpleNamespace(phase="regions", file_hash="frozen", fence_token=fence,
                           lease_owner=owner, lease_until=until)


def allowed(current, error, *, initial=0, worker="celery:a"):
    return content_build_failure_allowed(current, worker_id=worker, initial_fence=initial,
                                         file_hash="frozen", error=error, now=datetime(2026, 10, 12))


def test_duplicate_delivery_already_leased_never_revokes_even_same_task_id():
    current = job(until=datetime(2026, 10, 12) + timedelta(minutes=1))
    assert not allowed(current, ContentBuildError("content build is already leased"))


def test_late_exception_cannot_clear_new_worker_or_same_id_new_fence():
    assert not allowed(job(fence=2, owner="celery:b"), RuntimeError("late"))
    assert not allowed(job(fence=2), RuntimeError("late"))
    assert not allowed(job(fence=1), ContentBuildError("stale content build worker cannot submit"))


def test_owner_with_exact_claim_fence_can_persist_failure():
    assert allowed(job(fence=1), RuntimeError("provider failed"))


def test_preclaim_hash_failure_requires_unchanged_unleased_source_generation():
    assert allowed(job(fence=0, owner=None), ValueError("source hash invalid"))
    assert not allowed(job(fence=0, owner="celery:b", until=datetime(2026, 10, 12) + timedelta(seconds=1)),
                       ValueError("source hash invalid"))
    current = job(fence=0, owner=None)
    current.file_hash = "replacement"
    assert not allowed(current, ValueError("source hash invalid"))
