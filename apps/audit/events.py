"""
Registry of audit event types.

Every recorded action must be registered here with its category, kind and the
metadata keys it may carry. Anything else is rejected, so new modules cannot
log free-form or sensitive data by accident.

Kinds:
    BUSINESS  - written in the caller's transaction; commits or rolls back with
                the business change. Mandatory: failures propagate.
    SECURITY  - failed attempts, denials and sign-in activity; must survive a
                rollback of the rejected operation (see apps.audit.services).
"""

from dataclasses import dataclass

BUSINESS = "BUSINESS"
SECURITY = "SECURITY"


class Category:
    AUTHENTICATION = "AUTHENTICATION"
    ACCOUNT = "ACCOUNT"
    AUTHORIZATION = "AUTHORIZATION"
    CONFIGURATION = "CONFIGURATION"
    AUDIT = "AUDIT"
    LEDGER = "LEDGER"
    OPERATIONS = "OPERATIONS"
    MEMBERSHIP = "MEMBERSHIP"
    CONTENT = "CONTENT"
    ADVERTISING = "ADVERTISING"

    choices = [
        (AUTHENTICATION, "Authentication"),
        (ACCOUNT, "Account"),
        (AUTHORIZATION, "Authorization"),
        (CONFIGURATION, "Configuration"),
        (AUDIT, "Audit"),
        (LEDGER, "Ledger"),
        (OPERATIONS, "Operations"),
        (MEMBERSHIP, "Membership"),
        (CONTENT, "Content"),
        (ADVERTISING, "Advertising"),
    ]


@dataclass(frozen=True)
class EventType:
    action: str
    category: str
    kind: str
    metadata_keys: frozenset = frozenset()
    description: str = ""


def _event(action, category, kind, keys=(), description=""):
    return EventType(action, category, kind, frozenset(keys), description)


EVENT_TYPES = {
    event.action: event
    for event in (
        # Authentication (sign-in activity is security-kind: no business transaction).
        _event("auth.login.succeeded", Category.AUTHENTICATION, SECURITY, ["interface"]),
        _event("auth.login.failed", Category.AUTHENTICATION, SECURITY,
               ["interface", "identifier_hash", "account_exists"]),
        _event("auth.login.throttled", Category.AUTHENTICATION, SECURITY,
               ["interface", "identifier_hash"]),
        _event("auth.logout", Category.AUTHENTICATION, SECURITY),
        _event("auth.password_reset.requested", Category.AUTHENTICATION, SECURITY,
               ["identifier_hash", "throttled"]),
        _event("auth.password_reset.completed", Category.AUTHENTICATION, BUSINESS),
        _event("auth.password.changed", Category.AUTHENTICATION, BUSINESS),
        # Account lifecycle
        _event("account.registration.started", Category.ACCOUNT, BUSINESS),
        _event("account.registration.repeated", Category.ACCOUNT, SECURITY, ["account_status"]),
        _event("account.activated", Category.ACCOUNT, BUSINESS),
        _event("account.email_confirmed", Category.ACCOUNT, BUSINESS),
        _event("account.created_by_staff", Category.ACCOUNT, BUSINESS, ["role", "status"]),
        _event("account.role_changed", Category.ACCOUNT, BUSINESS, ["from_role", "to_role"]),
        _event("account.status_changed", Category.ACCOUNT, BUSINESS, ["from_status", "to_status"]),
        _event("account.deleted", Category.ACCOUNT, BUSINESS, ["role", "status"]),
        _event("account.access_change.denied", Category.AUTHORIZATION, SECURITY,
               ["requested_role", "requested_status", "reasons"]),
        # Generic authorization denials (e.g. admin 403 on a write request).
        _event("authorization.denied", Category.AUTHORIZATION, SECURITY, ["method", "view"]),
        # Configuration
        _event("config.updated", Category.CONFIGURATION, BUSINESS,
               ["version", "fields", "changes", "feature_flags", "maintenance_mode"]),
        _event("config.update.denied", Category.CONFIGURATION, SECURITY, ["fields", "reason"]),
        # Audit administration
        _event("audit.exported", Category.AUDIT, SECURITY, ["count", "start", "end"]),
        _event("audit.purged", Category.AUDIT, BUSINESS, ["count", "before"]),
        # Financial ledger (amounts are whole shillings; never payment credentials)
        _event("ledger.account.opened", Category.LEDGER, BUSINESS, ["account_type", "scope_type"]),
        _event("ledger.account.status_changed", Category.LEDGER, BUSINESS, ["from_status", "to_status"]),
        _event("ledger.entry.posted", Category.LEDGER, BUSINESS,
               ["posting_type", "total_amount", "line_count", "source_type"]),
        _event("ledger.entry.reversed", Category.LEDGER, BUSINESS,
               ["posting_type", "total_amount", "original_entry"]),
        _event("ledger.posting.rejected", Category.LEDGER, SECURITY, ["posting_type", "reason", "source_type"]),
        # Background processing (routine successes are not audited: no noise)
        _event("jobs.job.failed", Category.OPERATIONS, BUSINESS, ["job_type", "attempts", "error_class"]),
        _event("jobs.job.retried", Category.OPERATIONS, BUSINESS, ["job_type", "previous_status"]),
        _event("jobs.job.cancelled", Category.OPERATIONS, BUSINESS, ["job_type", "previous_status"]),
        _event("jobs.schedule.changed", Category.OPERATIONS, BUSINESS, ["schedule", "enabled"]),
        _event("notifications.email.failed", Category.OPERATIONS, BUSINESS, ["kind", "error_class", "attempts"]),
        # Memberships
        _event("membership.plan.created", Category.MEMBERSHIP, BUSINESS, ["code", "price_amount", "duration_days"]),
        _event("membership.plan.updated", Category.MEMBERSHIP, BUSINESS,
               ["fields", "version", "price_amount", "new_version"]),
        _event("membership.plan.status_changed", Category.MEMBERSHIP, BUSINESS, ["from_status", "to_status"]),
        _event("membership.subscription.created", Category.MEMBERSHIP, BUSINESS,
               ["plan_code", "price_amount", "renewal"]),
        *[_event(f"membership.subscription.{name}", Category.MEMBERSHIP, BUSINESS,
                 ["from_status", "to_status", "reason", "plan_code", "source", "queued"])
          for name in ("activated", "cancelled", "suspended", "reinstated", "expired")],
        _event("membership.action.denied", Category.MEMBERSHIP, SECURITY, ["action", "reason"]),
        _event("membership.activation.rejected", Category.MEMBERSHIP, SECURITY, ["reason", "subscription"]),
        # Video content (routine playback is not audited)
        _event("video.category.created", Category.CONTENT, BUSINESS, ["slug"]),
        _event("video.category.updated", Category.CONTENT, BUSINESS, ["slug", "fields"]),
        _event("video.category.status_changed", Category.CONTENT, BUSINESS, ["slug", "active"]),
        _event("video.created", Category.CONTENT, BUSINESS, ["provider", "slug"]),
        _event("video.updated", Category.CONTENT, BUSINESS, ["fields"]),
        _event("video.upload.initiated", Category.CONTENT, BUSINESS, ["content_type", "size_mb"]),
        _event("video.upload.completed", Category.CONTENT, BUSINESS),
        _event("video.upload.failed", Category.CONTENT, BUSINESS, ["reason"]),
        _event("video.processing.completed", Category.CONTENT, BUSINESS, ["duration_seconds"]),
        _event("video.processing.failed", Category.CONTENT, BUSINESS, ["status_code"]),
        _event("video.published", Category.CONTENT, BUSINESS, ["from_status"]),
        _event("video.unpublished", Category.CONTENT, BUSINESS, ["from_status"]),
        _event("video.archived", Category.CONTENT, BUSINESS, ["from_status"]),
        _event("video.media.deletion_requested", Category.CONTENT, BUSINESS),
        _event("video.media.deleted", Category.CONTENT, BUSINESS),
        _event("video.provider.sync_failed", Category.CONTENT, BUSINESS, ["job_type", "error_class"]),
        _event("video.action.denied", Category.CONTENT, SECURITY, ["action", "reason"]),
        # Advertisers and campaigns (demo budgets are not money; no payment data)
        _event("advertiser.created", Category.ADVERTISING, BUSINESS, ["country"]),
        _event("advertiser.updated", Category.ADVERTISING, BUSINESS, ["fields"]),
        _event("advertiser.resubmitted", Category.ADVERTISING, BUSINESS),
        *[_event(f"advertiser.verification.{name}", Category.ADVERTISING, BUSINESS, ["from_status", "reason"])
          for name in ("approved", "rejected", "suspended", "reinstated")],
        _event("advertiser.member.invited", Category.ADVERTISING, BUSINESS, ["role"]),
        _event("advertiser.member.joined", Category.ADVERTISING, BUSINESS, ["role"]),
        _event("advertiser.member.removed", Category.ADVERTISING, BUSINESS, ["role"]),
        _event("advertiser.invitation.declined", Category.ADVERTISING, BUSINESS),
        _event("advertiser.invitation.revoked", Category.ADVERTISING, BUSINESS),
        _event("advertiser.video.granted", Category.ADVERTISING, BUSINESS, ["video"]),
        _event("advertiser.video.revoked", Category.ADVERTISING, BUSINESS, ["video"]),
        _event("advertiser.action.denied", Category.ADVERTISING, SECURITY, ["action", "reason"]),
        _event("campaign.created", Category.ADVERTISING, BUSINESS, ["revision"]),
        _event("campaign.updated", Category.ADVERTISING, BUSINESS, ["revision", "fields"]),
        *[_event(f"campaign.{name}", Category.ADVERTISING, BUSINESS, ["from_status", "revision", "reason"])
          for name in ("submitted", "withdrawn", "approved", "rejected", "activated", "paused", "resumed",
                       "completed", "cancelled", "archived", "reopened")],
        _event("campaign.funding.refused", Category.ADVERTISING, SECURITY, ["requested_status", "reason"]),
    )
}


class UnknownAuditEventError(LookupError):
    pass


def get_event_type(action):
    try:
        return EVENT_TYPES[action]
    except KeyError:
        raise UnknownAuditEventError(f"Unregistered audit action: {action!r}") from None
