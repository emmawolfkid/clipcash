from django.core.exceptions import PermissionDenied
from django.urls import reverse

from apps.accounts.throttling import client_ip

from . import context as audit_context
from .models import AuditEvent
from .services import DENIED, flush_pending_security_events, record_security_event

SAFE_METHODS = {"GET", "HEAD", "OPTIONS", "TRACE"}


class AuditContextMiddleware:
    """
    Sets up the audit context for each request: a correlation id (also returned
    as X-Request-ID), the source, client IP and user agent. Flushes deferred
    security events after the view and its transactions have finished, and
    records otherwise-unrecorded PermissionDenied on write requests.

    Incoming X-Request-ID headers are ignored: they are client-controlled.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        admin_prefix = reverse("admin:index")
        context = audit_context.AuditContext(
            request_id=audit_context.new_request_id(),
            source=AuditEvent.Source.ADMIN if request.path.startswith(admin_prefix) else AuditEvent.Source.WEB,
            request=request,
            ip_address=client_ip(request) or None,
            user_agent=request.META.get("HTTP_USER_AGENT", "")[:256],
        )
        request.request_id = context.request_id
        token = audit_context.activate(context)
        try:
            response = self.get_response(request)
            response["X-Request-ID"] = context.request_id
            return response
        finally:
            try:
                flush_pending_security_events(context)
            finally:
                audit_context.deactivate(token)

    def process_exception(self, request, exception):
        context = audit_context.current()
        if (
            isinstance(exception, PermissionDenied)
            and request.method not in SAFE_METHODS
            and context is not None
            and not context.denial_recorded
        ):
            match = getattr(request, "resolver_match", None)
            record_security_event(
                "authorization.denied",
                outcome=DENIED,
                metadata={"method": request.method, "view": (match.view_name if match else "")[:200]},
            )
        return None
