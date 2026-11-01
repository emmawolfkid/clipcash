"""Per-request (or per-command) audit context held in a context variable."""

import contextvars
import re
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field

_REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9._-]{8,64}$")

_current = contextvars.ContextVar("clipcash_audit_context", default=None)


@dataclass
class AuditContext:
    request_id: str
    source: str
    request: object = None
    ip_address: str | None = None
    user_agent: str = ""
    pending: list = field(default_factory=list)
    denial_recorded: bool = False


def new_request_id():
    return uuid.uuid4().hex


def valid_request_id(value):
    return bool(value) and bool(_REQUEST_ID_RE.match(value))


def current():
    return _current.get()


def activate(context):
    return _current.set(context)


def deactivate(token):
    _current.reset(token)


@contextmanager
def audit_scope(source="SYSTEM", request_id=None):
    """
    Audit context for code outside a web request (management commands, future
    background jobs). Pending security events are flushed when the scope ends,
    including after an exception.
    """
    from .services import flush_pending_security_events

    context = AuditContext(request_id=request_id or new_request_id(), source=source)
    token = activate(context)
    try:
        yield context
    finally:
        try:
            flush_pending_security_events(context)
        finally:
            deactivate(token)
