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
    VIEWING = "VIEWING"
    REWARDS = "REWARDS"
    PAYMENTS = "PAYMENTS"
    WALLETS = "WALLETS"
    WITHDRAWALS = "WITHDRAWALS"
    SUPPORT = "SUPPORT"
    REPORTS = "REPORTS"

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
        (VIEWING, "Viewing"),
        (REWARDS, "Demo rewards"),
        (PAYMENTS, "Payments (sandbox)"),
        (WALLETS, "Wallets"),
        (WITHDRAWALS, "Withdrawals"),
        (SUPPORT, "Customer support"),
        (REPORTS, "Reports"),
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
        _event("auth.current_password.failed", Category.AUTHENTICATION, SECURITY, ["operation", "throttled"]),
        # Account lifecycle
        _event("account.registration.started", Category.ACCOUNT, BUSINESS),
        _event("account.registration.repeated", Category.ACCOUNT, SECURITY, ["account_status"]),
        _event("account.activated", Category.ACCOUNT, BUSINESS),
        _event("account.email_confirmed", Category.ACCOUNT, BUSINESS),
        _event("account.profile_updated", Category.ACCOUNT, BUSINESS, ["changed_fields"]),
        _event("account.email_change.requested", Category.ACCOUNT, BUSINESS, ["new_address_hash", "address_available"]),
        _event("account.email_change.denied", Category.ACCOUNT, SECURITY, ["reason"]),
        _event("account.email_change.cancelled", Category.ACCOUNT, BUSINESS),
        _event("account.email_changed", Category.ACCOUNT, BUSINESS, ["previous_address_hash", "new_address_hash"]),
        _event("account.created_by_staff", Category.ACCOUNT, BUSINESS, ["role", "status"]),
        _event("account.role_changed", Category.ACCOUNT, BUSINESS, ["from_role", "to_role"]),
        _event("account.status_changed", Category.ACCOUNT, BUSINESS, ["from_status", "to_status"]),
        _event("account.deleted", Category.ACCOUNT, BUSINESS, ["role", "status"]),
        _event("account.access_change.denied", Category.AUTHORIZATION, SECURITY,
               ["requested_role", "requested_status", "reasons"]),
        # Generic authorization denials (e.g. admin 403 on a write request).
        _event("authorization.denied", Category.AUTHORIZATION, SECURITY, ["method", "view"]),
        _event("authorization.roles.provisioned", Category.AUTHORIZATION, BUSINESS,
               ["permissions_added", "group_members_added", "group_members_removed"]),
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
        _event("membership.complimentary.granted", Category.MEMBERSHIP, BUSINESS,
               ["member", "plan_code", "starts_at", "expires_at", "reason", "operation"]),
        _event("membership.action.denied", Category.MEMBERSHIP, SECURITY, ["action", "reason"]),
        _event("membership.activation.rejected", Category.MEMBERSHIP, SECURITY, ["reason", "subscription"]),
        # PAY-01: manual (Airtel Lipa) membership payments. Phone numbers are never recorded.
        _event("membership.manual_payment.submitted", Category.MEMBERSHIP, BUSINESS,
               ["reference", "amount", "plan_code", "resubmission"]),
        _event("membership.manual_payment.cancelled", Category.MEMBERSHIP, BUSINESS, ["reference"]),
        _event("membership.manual_payment.approved", Category.MEMBERSHIP, BUSINESS,
               ["reference", "amount", "plan_code", "ledger_entry", "subscription"]),
        _event("membership.manual_payment.rejected", Category.MEMBERSHIP, BUSINESS, ["reference", "reason"]),
        _event("membership.manual_payment.refused", Category.MEMBERSHIP, SECURITY, ["action", "reason"]),
        _event("membership.payment_instructions.published", Category.MEMBERSHIP, BUSINESS,
               ["method", "business_number", "recipient_name", "replaced"]),
        _event("membership.payment_instructions.retired", Category.MEMBERSHIP, BUSINESS, ["method"]),
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
        # Verified viewing (evidence summaries only; never playback URLs or raw telemetry)
        _event("viewing.session.started", Category.VIEWING, BUSINESS, ["campaign", "revision"]),
        _event("viewing.session.denied", Category.VIEWING, SECURITY, ["campaign", "reason"]),
        _event("viewing.session.expired", Category.VIEWING, BUSINESS, ["campaign"]),
        _event("viewing.session.invalidated", Category.VIEWING, BUSINESS, ["campaign", "reason"]),
        _event("viewing.session.finalized", Category.VIEWING, BUSINESS,
               ["campaign", "outcome", "reasons", "credited_seconds", "required_seconds", "trigger"]),
        _event("viewing.evidence.rejected", Category.VIEWING, SECURITY, ["campaign", "reason", "count"]),
        _event("viewing.fraud.flagged", Category.VIEWING, SECURITY, ["campaign", "flags"]),
        _event("viewing.review.decided", Category.VIEWING, BUSINESS, ["decision", "reason", "flags"]),
        _event("viewing.session.revoked", Category.VIEWING, BUSINESS, ["campaign", "reason"]),
        # Demo rewards (simulated, never money)
        _event("rewards.policy.created", Category.REWARDS, BUSINESS, ["campaign", "version", "amount"]),
        _event("rewards.policy.approved", Category.REWARDS, BUSINESS, ["campaign", "version", "amount", "replaces"]),
        _event("rewards.policy.rejected", Category.REWARDS, BUSINESS, ["campaign", "version", "reason"]),
        _event("rewards.policy.retired", Category.REWARDS, BUSINESS, ["campaign", "version", "reason"]),
        _event("rewards.decision.qualified", Category.REWARDS, BUSINESS,
               ["campaign", "policy_version", "amount", "demo_consumed"]),
        _event("rewards.decision.rejected", Category.REWARDS, BUSINESS, ["campaign", "reasons"]),
        _event("rewards.budget.exhausted", Category.REWARDS, BUSINESS, ["campaign", "demo_allocated",
                                                                        "demo_consumed", "amount"]),
        _event("rewards.reward.reversed", Category.REWARDS, BUSINESS, ["campaign", "amount", "source", "reason"]),
        _event("rewards.duplicate.refused", Category.REWARDS, SECURITY, ["campaign"]),
        _event("rewards.action.denied", Category.REWARDS, SECURITY, ["action", "reason"]),
        # Payments, SANDBOX (references and whole-TZS amounts only; never payloads or keys)
        _event("payments.intent.created", Category.PAYMENTS, BUSINESS, ["purpose", "amount", "environment"]),
        _event("payments.intent.cancelled", Category.PAYMENTS, BUSINESS, ["reference"]),
        _event("payments.intent.expired", Category.PAYMENTS, BUSINESS, ["reference"]),
        _event("payments.intent.failed", Category.PAYMENTS, BUSINESS, ["reference", "reason"]),
        _event("payments.callback.accepted", Category.PAYMENTS, BUSINESS, ["provider", "kind", "status"]),
        _event("payments.callback.refused", Category.PAYMENTS, SECURITY, ["provider", "reason"]),
        _event("payments.callback.duplicate", Category.PAYMENTS, SECURITY, ["provider", "kind"]),
        _event("payments.reconciliation.completed", Category.PAYMENTS, BUSINESS, ["reference", "amount"]),
        _event("payments.reconciliation.refused", Category.PAYMENTS, BUSINESS, ["reference", "reason"]),
        _event("payments.settlement.completed", Category.PAYMENTS, BUSINESS, ["reference", "amount", "purpose"]),
        _event("payments.sandbox.posted", Category.PAYMENTS, BUSINESS, ["posting_type", "amount", "funds_domain"]),
        _event("payments.exception.opened", Category.PAYMENTS, BUSINESS, ["reason", "amount"]),
        _event("payments.exception.resolved", Category.PAYMENTS, BUSINESS, ["reason", "action"]),
        _event("payments.allocation.created", Category.PAYMENTS, BUSINESS, ["campaign", "amount"]),
        _event("payments.allocation.released", Category.PAYMENTS, BUSINESS, ["campaign", "amount"]),
        _event("payments.allocation.refused", Category.PAYMENTS, SECURITY, ["campaign", "reason"]),
        _event("payments.refund.requested", Category.PAYMENTS, BUSINESS, ["reference", "amount"]),
        _event("payments.refund.completed", Category.PAYMENTS, BUSINESS, ["reference", "amount"]),
        _event("payments.refund.rejected", Category.PAYMENTS, BUSINESS, ["reference"]),
        _event("payments.dispute.opened", Category.PAYMENTS, BUSINESS, ["reference", "frozen", "shortfall"]),
        _event("payments.dispute.resolved", Category.PAYMENTS, BUSINESS, ["reference", "outcome"]),
        _event("payments.membership.preview", Category.PAYMENTS, BUSINESS, ["reference", "decision"]),
        _event("payments.sandbox.simulated", Category.PAYMENTS, BUSINESS, ["reference", "action"]),
        _event("payments.action.denied", Category.PAYMENTS, SECURITY, ["action", "reason"]),
        # Member wallets (no money in this phase)
        _event("wallets.wallet.created", Category.WALLETS, BUSINESS, ["number"]),
        _event("wallets.restriction.placed", Category.WALLETS, BUSINESS, ["kind", "reason"]),
        _event("wallets.restriction.lift_requested", Category.WALLETS, BUSINESS, ["kind", "reason"]),
        _event("wallets.restriction.lifted", Category.WALLETS, BUSINESS, ["kind", "note"]),
        _event("wallets.restriction.lift_declined", Category.WALLETS, BUSINESS, ["kind", "note"]),
        _event("wallets.wallet.inspected", Category.WALLETS, SECURITY, ["view"]),
        _event("wallets.access.denied", Category.WALLETS, SECURITY, ["action", "reason"]),
        _event("wallets.posting.refused", Category.WALLETS, SECURITY, ["category", "reason"]),
        _event("wallets.reconciliation.discrepancy", Category.WALLETS, BUSINESS, ["code"]),
        _event("wallets.discrepancy.resolved", Category.WALLETS, BUSINESS, ["code", "note"]),
        # Withdrawals: real ones locked; DEMO scenarios simulated (never account numbers)
        _event("withdrawals.beneficiary.created", Category.WITHDRAWALS, BUSINESS, ['kind', 'provider']),
        _event("withdrawals.beneficiary.deactivated", Category.WITHDRAWALS, BUSINESS, ['kind']),
        _event("withdrawals.beneficiary.demo_verified", Category.WITHDRAWALS, BUSINESS, ['kind']),
        _event("withdrawals.request.denied", Category.WITHDRAWALS, SECURITY, ['reasons']),
        _event("withdrawals.demo.created", Category.WITHDRAWALS, BUSINESS, ['reference', 'amount', 'high_risk', 'flags']),
        _event("withdrawals.demo.duplicate_refused", Category.WITHDRAWALS, SECURITY, ['reason']),
        _event("withdrawals.demo.review_started", Category.WITHDRAWALS, BUSINESS, ['reference']),
        _event("withdrawals.demo.approval_recorded", Category.WITHDRAWALS, BUSINESS, ['reference', 'approvals']),
        _event("withdrawals.demo.approved", Category.WITHDRAWALS, BUSINESS, ['reference']),
        _event("withdrawals.demo.rejected", Category.WITHDRAWALS, BUSINESS, ['reference', 'reason_code']),
        _event("withdrawals.demo.cancelled", Category.WITHDRAWALS, BUSINESS, ['reference', 'reason_code']),
        _event("withdrawals.demo.expired", Category.WITHDRAWALS, BUSINESS, ['reference']),
        _event("withdrawals.payout.initiated", Category.WITHDRAWALS, BUSINESS, ['reference']),
        _event("withdrawals.payout.completed", Category.WITHDRAWALS, BUSINESS, ['reference']),
        _event("withdrawals.payout.failed", Category.WITHDRAWALS, BUSINESS, ['reference']),
        _event("withdrawals.payout.uncertain", Category.WITHDRAWALS, BUSINESS, ['reference']),
        _event("withdrawals.payout.returned", Category.WITHDRAWALS, BUSINESS, ['reference']),
        _event("withdrawals.callback.refused", Category.WITHDRAWALS, SECURITY, ['reason']),
        _event("withdrawals.callback.duplicate", Category.WITHDRAWALS, SECURITY, ['kind']),
        _event("withdrawals.risk.flagged", Category.WITHDRAWALS, SECURITY, ['flags']),
        _event("withdrawals.exception.opened", Category.WITHDRAWALS, BUSINESS, ['code']),
        _event("withdrawals.exception.resolved", Category.WITHDRAWALS, BUSINESS, ['code']),
        _event("withdrawals.sandbox.simulated", Category.WITHDRAWALS, BUSINESS, ['reference', 'action']),
        _event("withdrawals.action.denied", Category.WITHDRAWALS, SECURITY, ['action', 'reason']),
        # Customer support (references and states only; never message text or notes)
        _event("support.ticket.created", Category.SUPPORT, BUSINESS, ["reference", "category", "organization"]),
        _event("support.ticket.customer_replied", Category.SUPPORT, BUSINESS, ["reference", "reopened"]),
        _event("support.ticket.staff_replied", Category.SUPPORT, BUSINESS, ["reference", "status"]),
        _event("support.ticket.note_added", Category.SUPPORT, BUSINESS, ["reference", "status"]),
        _event("support.ticket.assigned", Category.SUPPORT, BUSINESS, ["reference", "assignee"]),
        _event("support.ticket.status_changed", Category.SUPPORT, BUSINESS, ["reference", "from_status", "to_status"]),
        _event("support.ticket.priority_changed", Category.SUPPORT, BUSINESS,
               ["reference", "from_priority", "to_priority"]),
        _event("support.action.denied", Category.SUPPORT, SECURITY, ["action", "reason"]),
        # Reports (aggregates only; the export event names the report and period, never its rows)
        _event("reports.exported", Category.REPORTS, SECURITY, ["report", "start", "end", "rows"]),
        _event("reports.action.denied", Category.REPORTS, SECURITY, ["action", "report", "reason"]),
    )
}


class UnknownAuditEventError(LookupError):
    pass


def get_event_type(action):
    try:
        return EVENT_TYPES[action]
    except KeyError:
        raise UnknownAuditEventError(f"Unregistered audit action: {action!r}") from None
