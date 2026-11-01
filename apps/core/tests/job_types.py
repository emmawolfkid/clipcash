"""Job types used only by the test suite (registered when tests import this module)."""

import threading
from collections import Counter

from apps.core.jobs.errors import PermanentJobError, RetryableJobError
from apps.core.jobs.registry import register

EXECUTIONS = Counter()
_lock = threading.Lock()
FAILURE_HOOK_CALLS = []


@register("tests.noop", payload_schema={"value": "int"}, max_attempts=3)
def noop(payload, job):
    with _lock:
        EXECUTIONS[str(job.pk)] += 1


@register("tests.flaky", max_attempts=3, backoff_base_seconds=10, backoff_max_seconds=40,
          on_failure=lambda job: FAILURE_HOOK_CALLS.append(job.pk))
def flaky(payload, job):
    raise RetryableJobError("provider timeout")


@register("tests.permanent", max_attempts=5)
def permanent(payload, job):
    raise PermanentJobError("recipient rejected")


@register("tests.leaky", max_attempts=1)
def leaky(payload, job):
    raise ValueError("token=SECRET-123 for victim@example.com")


@register("tests.interrupt", max_attempts=3)
def interrupt(payload, job):
    raise KeyboardInterrupt


@register("tests.locked", max_attempts=2, admin_retryable=False)
def locked(payload, job):
    raise PermanentJobError("nope")
