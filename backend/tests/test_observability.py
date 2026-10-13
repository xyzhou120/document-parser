from datetime import datetime, timedelta
from types import SimpleNamespace

from app.api.observability import summarize_runs


def test_observability_reports_measured_duration_wait_and_unknown_resources():
    created = datetime(2026, 10, 9)
    runs = [SimpleNamespace(status="COMPLETED", duration_seconds=10, created_at=created,
        started_at=created + timedelta(seconds=2), extraction_profile={"agent_runtime": {
            "status": "PARTIAL", "token_usage": {"total_tokens": 100}}}),
        SimpleNamespace(status="FAILED", duration_seconds=None, created_at=created,
                        started_at=None, extraction_profile={})]
    result = summarize_runs(runs)
    assert result["duration"]["p95_seconds"] == 10
    assert result["queue_wait"]["mean_seconds"] == 2
    assert result["partial_or_unresolved"] == 1
    assert result["token_usage"] == {"measured_runs": 1, "total_tokens": 100}
    assert result["gpu_usage"] is result["actual_cost"] is None


def test_empty_observability_does_not_report_zero_latency():
    result = summarize_runs([])
    assert result["duration"]["p50_seconds"] is None
    assert result["token_usage"]["total_tokens"] is None
