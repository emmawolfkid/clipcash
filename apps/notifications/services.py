"""
Durable email outbox.

enqueue_email() writes an OutboundEmail and its delivery job in the caller's
transaction: if the business change rolls back (e.g. a failed registration),
neither exists and nothing is sent. The delivery job runs after commit,
retries transient SMTP failures with backoff, and fails terminally after the
job's maximum attempts or on a permanent rejection.

Email kinds are registered by the owning app (e.g. apps.accounts.emails) with
a builder that renders the message at send time. A builder can skip a message
that no longer applies (account changed, already activated) by raising
SkipEmail. Messages are never sent after ``expires_at``.
"""

import logging
import re
import smtplib
from dataclasses import dataclass
from datetime import timedelta
from typing import Callable

from django.conf import settings
from django.core.mail import send_mail
from django.db import transaction
from django.utils import timezone

from apps.audit.services import record_business_event
from apps.core.jobs.errors import PermanentJobError, RetryableJobError
from apps.core.jobs.registry import register
from apps.core.jobs.services import enqueue

from .models import OutboundEmail

log = logging.getLogger("clipcash.jobs")

DELIVERY_JOB = "notifications.deliver_email"
_BASE_URL_RE = re.compile(r"^https?://[A-Za-z0-9.\-:\[\]]{1,190}$")


class SkipEmail(Exception):
    """Raised by a builder when the message no longer applies."""

    def __init__(self, reason):
        super().__init__(reason)
        self.reason = str(reason)[:60]


@dataclass(frozen=True)
class EmailKind:
    name: str
    builder: Callable  # builder(email) -> (subject, body)
    ttl_seconds: Callable  # () -> seconds the message stays deliverable
    context_keys: frozenset


EMAIL_KINDS = {}


def register_email_kind(name, *, builder, ttl_seconds, context_keys=("base_url",)):
    EMAIL_KINDS[name] = EmailKind(name, builder, ttl_seconds, frozenset(context_keys))


def _clean_context(kind, context):
    context = dict(context or {})
    if set(context) != set(kind.context_keys):
        raise ValueError(f"{kind.name} context must have exactly {sorted(kind.context_keys)}.")
    base_url = context.get("base_url")
    if base_url is not None and not _BASE_URL_RE.match(base_url):
        raise ValueError("base_url must be a scheme and host, e.g. https://clipcash.example.")
    for key, value in context.items():
        if not isinstance(value, (str, int, bool)) or (isinstance(value, str) and len(value) > 200):
            raise ValueError(f"Unsupported context value for {key!r}.")
    return context


def base_url(request):
    """Scheme and host of a validated request (ALLOWED_HOSTS already applied)."""
    return f"{request.scheme}://{request.get_host()}"


def enqueue_email(kind_name, *, recipient, user=None, context=None):
    """Queue one email in the caller's transaction. Returns the OutboundEmail."""
    kind = EMAIL_KINDS[kind_name]
    cleaned = _clean_context(kind, context)
    now = timezone.now()
    with transaction.atomic():
        email = OutboundEmail.objects.create(
            kind=kind_name,
            recipient=recipient,
            user_id=getattr(user, "pk", None),
            context=cleaned,
            token_issued_at=now,
            # At least one second, so the expiry check constraint always holds.
            expires_at=now + timedelta(seconds=max(1, kind.ttl_seconds())),
        )
        enqueue(DELIVERY_JOB, {"email_id": str(email.pk)}, idempotency_key=f"email:{email.pk}")
    return email


# --- Delivery -------------------------------------------------------------------

PERMANENT_SMTP_ERRORS = (smtplib.SMTPRecipientsRefused, smtplib.SMTPSenderRefused)


def _mark_failed(job):
    """on_failure hook: the delivery job failed terminally."""
    email = OutboundEmail.objects.select_for_update().filter(pk=job.payload.get("email_id")).first()
    if email is None or email.status != OutboundEmail.Status.PENDING:
        return
    email.status = OutboundEmail.Status.FAILED
    email.attempts = job.attempts
    email.last_error_class = job.last_error_class or "UNEXPECTED"
    email.save(update_fields=["status", "attempts", "last_error_class", "updated_at"])
    record_business_event(
        "notifications.email.failed", actor=None, target=email,
        metadata={"kind": email.kind, "error_class": email.last_error_class, "attempts": email.attempts},
    )


@register(DELIVERY_JOB, payload_schema={"email_id": "uuid"}, max_attempts=6, priority=10,
          backoff_base_seconds=60, backoff_max_seconds=3600, on_failure=_mark_failed,
          description="Deliver one outbox email.")
def deliver_email(payload, job):
    email = OutboundEmail.objects.select_for_update().filter(pk=payload["email_id"]).first()
    if email is None or email.status != OutboundEmail.Status.PENDING:
        return  # Already handled (idempotent re-run) or cancelled.
    now = timezone.now()
    if now >= email.expires_at:
        email.status = OutboundEmail.Status.EXPIRED
        email.save(update_fields=["status", "updated_at"])
        log.warning("email_expired id=%s kind=%s", email.pk, email.kind)
        return
    kind = EMAIL_KINDS.get(email.kind)
    if kind is None:
        raise PermanentJobError(f"Unknown email kind {email.kind}")
    try:
        subject, body = kind.builder(email)
    except SkipEmail as skip:
        email.status = OutboundEmail.Status.SKIPPED
        email.skip_reason = skip.reason
        email.save(update_fields=["status", "skip_reason", "updated_at"])
        return

    try:
        send_mail(" ".join(subject.split()), body, settings.DEFAULT_FROM_EMAIL, [email.recipient],
                  fail_silently=False)
    except PERMANENT_SMTP_ERRORS as error:
        # Never store the server's message: it can echo addresses or content.
        raise PermanentJobError(f"SMTP rejected the message ({type(error).__name__})") from None
    except smtplib.SMTPDataError as error:
        if getattr(error, "smtp_code", 0) >= 500:
            raise PermanentJobError("SMTP rejected the message (SMTPDataError)") from None
        raise RetryableJobError("SMTP temporarily refused the message (SMTPDataError)") from None
    except (smtplib.SMTPException, OSError) as error:
        raise RetryableJobError(f"SMTP unavailable ({type(error).__name__})") from None
    email.status = OutboundEmail.Status.SENT
    email.sent_at = timezone.now()
    email.attempts = job.attempts
    email.last_error_class = ""
    email.save(update_fields=["status", "sent_at", "attempts", "last_error_class", "updated_at"])


@register("notifications.prune_outbox", max_attempts=2, admin_retryable=False,
          description="Delete delivered, expired, skipped or failed emails past retention.")
def prune_outbox(payload, job):
    cutoff = timezone.now() - timedelta(days=settings.NOTIFICATIONS_OUTBOX_RETENTION_DAYS)
    OutboundEmail.objects.exclude(status=OutboundEmail.Status.PENDING).filter(created_at__lt=cutoff).delete()
