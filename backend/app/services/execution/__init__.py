"""Reliable extraction execution primitives.

The modules in this package deliberately do not start Celery tasks.  They are
transactional building blocks used by the integration layer (P13).
"""

from .dispatch_outbox import DispatchOutbox
from .field_scheduler import (
    FieldExecutionError,
    FieldSchedulerRunner,
    LeaseToken,
    QuantumOutcome,
    RunControlError,
    Scheduler,
    StaleLeaseError,
    aggregate_run_status,
    wakeup_dedupe_key,
)
from .job_repository import JobRepository

__all__ = [
    "DispatchOutbox",
    "FieldExecutionError",
    "FieldSchedulerRunner",
    "JobRepository",
    "LeaseToken",
    "QuantumOutcome",
    "RunControlError",
    "Scheduler",
    "StaleLeaseError",
    "aggregate_run_status",
    "wakeup_dedupe_key",
]
