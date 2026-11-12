"""
The only supported way to write audit events.

record_business_event()
    Must be called inside the business transaction (transaction.atomic). The
    event commits or rolls back with the change. Validation and database errors
    propagate, so a business change can never commit without its audit record.

record_security_event()
    For sign-in activity, failed attempts and denials, which must survive a
    rollback of the rejected operation. Outside a transaction it is written
    immediately (autocommit). Inside one, with an active audit context
    (every web request; audit_scope() elsewhere), it is held in memory and
    written when the request or scope ends, after the transaction has finished,
    so a rollback cannot discard it. Write failures are logged loudly but do not
    break the response: a failed sign-in page must still render.

Every event also produces one ``clipcash.security`` log line (ids and codes only).
"""

import logging
import numbers

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import DatabaseError, IntegrityError, connection, transaction
from django.utils.crypto import salted_hmac

from . import context as audit_context
from .events import BUSINESS, SECURITY, get_event_type
from .models import AuditEvent

security_log = logging.getLogger("clipcash.security")

SUCCESS = AuditEvent.Outcome.SUCCESS
FAILURE = AuditEvent.Outcome.FAILURE
DENIED = AuditEvent.Outcome.DENIED

# Key fragments that must never appear in metadata, at any depth, even if a
# developer adds them to an event's allowlist by mistake.
FORBIDDEN_KEY_FRAGMENTS = (
    "password", "passwd", "secret", "token", "cookie", "session", "csrf", "otp",
    "pin", "card", "cvv", "iban", "account_number", "authorization", "api_key", "private",
)
MAX_STRING = 300
MAX_ITEMS = 50
MAX_DEPTH = 3

_UNSET = object()


class AuditError(RuntimeError):
    """An audit event could not be recorded as requested."""


# --- Validation ---------------------------------------------------------------


def _check_key(key, path):
    if not isinstance(key, str) or not key or len(key) > 64:
        raise ValidationError(f"Invalid metadata key at {path or 'root'}.")
    lowered = key.lower()
    for fragment in FORBIDDEN_KEY_FRAGMENTS:
        if fragment in lowered:
            raise ValidationError(f"Sensitive metadata key not allowed: {key!r}.")


def _clean_value(value, path, depth):
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, numbers.Integral):
        return int(value)
    if isinstance(value, float):
        raise ValidationError(f"Floats are not allowed in audit metadata ({path}).")
    if isinstance(value, str):
        if len(value) > MAX_STRING:
            raise ValidationError(f"Metadata string too long at {path}.")
        return value
    if depth >= MAX_DEPTH:
        raise ValidationError(f"Metadata nested too deeply at {path}.")
    if isinstance(value, (list, tuple)):
        if len(value) > MAX_ITEMS:
            raise ValidationError(f"Too many metadata items at {path}.")
        return [_clean_value(item, f"{path}[]", depth + 1) for item in value]
    if isinstance(value, dict):
        if len(value) > MAX_ITEMS:
            raise ValidationError(f"Too many metadata keys at {path}.")
        cleaned = {}
        for key, item in value.items():
            _check_key(key, path)
            cleaned[key] = _clean_value(item, f"{path}.{key}", depth + 1)
        return cleaned
    raise ValidationError(f"Unsupported metadata value type at {path}: {type(value).__name__}.")


def clean_metadata(event_type, metadata):
    metadata = metadata or {}
    if not isinstance(metadata, dict):
        raise ValidationError("Audit metadata must be a dict.")
    unknown = set(metadata) - event_type.metadata_keys
    if unknown:
        raise ValidationError(
            f"Metadata keys not allowed for {event_type.action}: {', '.join(sorted(unknown))}."
        )
    cleaned = {}
    for key, value in metadata.items():
        _check_key(key, "")
        cleaned[key] = _clean_value(value, key, 1)
    return cleaned


def hash_identifier(value):
    """
    Stable keyed hash of a typed identifier (e.g. an attempted login email), so
    repeated attempts can be correlated without storing what was typed (users
    sometimes type their password into the email field). Changes if
    DJANGO_SECRET_KEY is rotated.
    """
    normalized = (value or "").strip().lower()
    if not normalized:
        return ""
    return salted_hmac("apps.audit.identifier", normalized).hexdigest()[:32]


# --- Building events -------------------------------------------------------------


def _actor_snapshot(actor):
    if actor is None or not getattr(actor, "is_authenticated", False):
        return None, "", ""
    # REG-02: phone-only members are recorded by their sign-in number.
    identifier = getattr(actor, "email", None) or getattr(actor, "phone_number", None) or ""
    return actor.pk, identifier[:254], str(getattr(actor, "role", ""))[:20]


def _target_snapshot(target):
    if target is None:
        return "", "", "", ""
    meta = target._meta
    return meta.app_label, meta.model_name, str(target.pk)[:64], str(target)[:200]


def _build(action, *, kind, outcome, actor, target, metadata, idempotency_key, source):
    event_type = get_event_type(action)
    if event_type.kind != kind:
        raise AuditError(f"{action} is a {event_type.kind} event; use the matching record function.")
    if outcome not in AuditEvent.Outcome.values:
        raise ValidationError(f"Invalid audit outcome: {outcome!r}.")

    context = audit_context.current()
    request = context.request if context else None
    if actor is _UNSET:
        actor = getattr(request, "user", None) if request is not None else None

    actor_id, actor_email, actor_role = _actor_snapshot(actor)
    app_label, model_name, target_id, target_repr = _target_snapshot(target)

    return AuditEvent(
        category=event_type.category,
        action=action,
        outcome=outcome,
        source=source or (context.source if context else AuditEvent.Source.SYSTEM),
        actor_id=actor_id,
        actor_email=actor_email,
        actor_role=actor_role,
        target_app_label=app_label,
        target_model=model_name,
        target_id=target_id,
        target_repr=target_repr,
        request_id=context.request_id if context else "",
        ip_address=(context.ip_address if context and settings.AUDIT_RECORD_IP_ADDRESS else None) or None,
        user_agent=(context.user_agent if context and settings.AUDIT_RECORD_USER_AGENT else "")[:256],
        metadata=clean_metadata(event_type, metadata),
        idempotency_key=idempotency_key or None,
    )


def _log(event):
    security_log.info(
        "audit action=%s outcome=%s actor=%s target=%s:%s request=%s",
        event.action,
        event.outcome,
        event.actor_id or "-",
        event.target_model or "-",
        event.target_id or "-",
        event.request_id or "-",
    )


def _insert(event):
    """Insert, honouring idempotency keys. Returns the stored event."""
    if event.idempotency_key:
        existing = AuditEvent.objects.filter(idempotency_key=event.idempotency_key).first()
        if existing is not None:
            return existing
        try:
            with transaction.atomic():
                event.save(force_insert=True)
        except IntegrityError:
            # A concurrent writer stored the same key first.
            return AuditEvent.objects.get(idempotency_key=event.idempotency_key)
    else:
        event.save(force_insert=True)
    _log(event)
    return event


# --- Public API -----------------------------------------------------------------


def record_business_event(action, *, outcome=SUCCESS, actor=_UNSET, target=None,
                          metadata=None, idempotency_key=None, source=None):
    """Record inside the business transaction. Errors propagate."""
    if not connection.in_atomic_block:
        raise AuditError(f"{action} must be recorded inside the business transaction.")
    event = _build(action, kind=BUSINESS, outcome=outcome, actor=actor, target=target,
                   metadata=metadata, idempotency_key=idempotency_key, source=source)
    return _insert(event)


def record_security_event(action, *, outcome, actor=_UNSET, target=None, metadata=None,
                          idempotency_key=None, source=None):
    """
    Record an event that must survive rollback. Validation errors (programming
    mistakes) propagate; database errors are logged and swallowed.
    Returns the event (unsaved while pending), or None if writing failed.
    """
    event = _build(action, kind=SECURITY, outcome=outcome, actor=actor, target=target,
                   metadata=metadata, idempotency_key=idempotency_key, source=source)
    context = audit_context.current()
    if outcome == DENIED and context is not None:
        context.denial_recorded = True
    if connection.in_atomic_block and context is not None:
        context.pending.append(event)
        return event
    return _write_security_event(event)


def _write_security_event(event):
    try:
        return _insert(event)
    except DatabaseError:
        security_log.exception(
            "audit_write_failed action=%s outcome=%s request=%s",
            event.action, event.outcome, event.request_id or "-",
        )
        return None


def flush_pending_security_events(context=None):
    """Write security events deferred during a transaction. Called by the
    middleware and audit_scope() once the work has finished."""
    context = context or audit_context.current()
    if context is None:
        return 0
    pending, context.pending = context.pending, []
    written = 0
    for event in pending:
        if _write_security_event(event) is not None:
            written += 1
    return written
