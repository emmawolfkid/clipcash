"""Exceptions job handlers raise to control retries.

Only ``safe_message`` from these exceptions is ever stored; messages of other
exceptions are discarded (they may contain addresses, URLs or tokens) and only
the exception class name is kept.
"""


class JobError(Exception):
    error_class = "UNEXPECTED"

    def __init__(self, safe_message=""):
        super().__init__(safe_message)
        self.safe_message = str(safe_message)[:300]


class RetryableJobError(JobError):
    """Temporary failure: retry with backoff (e.g. SMTP server unavailable)."""

    error_class = "TRANSIENT"


class PermanentJobError(JobError):
    """Retrying cannot help (e.g. recipient rejected): fail immediately."""

    error_class = "PERMANENT"
