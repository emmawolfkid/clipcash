"""
Administration read model (Phase 12): executive overview, operational health,
member directory and security summary.

Rules
-----
* Read-only. GET pages built from this module create no rows, audit events,
  jobs or decisions, and never call external services (Bunny, payment or payout
  providers). Tests enforce this.
* Permission-aware. Each section is computed only if the staff member holds the
  permission of the owning area (the same checks the area's own pages use);
  otherwise it is omitted, never shown with partial data.
* Separate money semantics. Sandbox payment totals, simulated demo rewards and
  real wallet balances are reported in separate sections and never added up.
* Bounded. Aggregates and counts, small ``[:N]`` lists with select_related, and
  time-bounded audit queries on indexed columns.
"""

from dataclasses import dataclass
from datetime import timedelta

from django.conf import settings
from django.contrib.auth import get_user_model
from django.db import connection
from django.db.models import Count, OuterRef, Q, Subquery, Sum
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from .labels import code_label

HEALTHY = "healthy"
ATTENTION = "attention"
NOT_CONFIGURED = "not_configured"
NOT_CHECKED = "not_checked"
STATUS_LABELS = {HEALTHY: "Healthy", ATTENTION: "Attention required", NOT_CONFIGURED: "Not configured",
                 NOT_CHECKED: "Not checked"}


@dataclass
class HealthCheck:
    key: str
    title: str
    status: str
    detail: str
    url_name: str = ""

    @property
    def label(self):
        return STATUS_LABELS[self.status]


def permissions(user):
    """What this staff member may see. Mirrors the owning areas' own checks."""
    from apps.administration.views import console_links
    from apps.memberships.services import is_super_admin

    can = console_links(user)
    can["operations"] = is_super_admin(user)
    return can


def _counts(qs, field):
    return dict(qs.values_list(field).annotate(n=Count("id")))


# --- Executive overview ----------------------------------------------------------------------


def overview(user):
    can = permissions(user)
    sections = {}
    if can["members"]:
        sections["members"] = _members()
    if can["advertisers"] or can["campaigns"]:
        sections["advertising"] = _advertising(can)
    if can["videos"]:
        sections["videos"] = _videos()
    if can["viewing"]:
        sections["viewing"] = _viewing()
    if can["rewards"]:
        sections["rewards"] = _rewards()
    if can["payments"]:
        sections["payments"] = _payments()
    if can["membership_payments"]:
        sections["membership_payments"] = _membership_payments()
    if can["wallets"]:
        sections["wallets"] = _wallets()
    if can["withdrawals"]:
        sections["withdrawals"] = _withdrawals()
    if can["operations"]:
        sections["jobs"] = _jobs()
    return can, sections


def attention_items(sections):
    """Open work for the overview, built only from the figures this staff member may see.
    Sandbox and demo items are named as such, and nothing is added across categories."""
    advertising = sections.get("advertising") or {}
    videos, viewing = sections.get("videos") or {}, sections.get("viewing") or {}
    rewards, payments = sections.get("rewards") or {}, sections.get("payments") or {}
    wallets, withdrawals = sections.get("wallets") or {}, sections.get("withdrawals") or {}
    membership_payments = sections.get("membership_payments") or {}
    rows = (
        (_("Membership payments awaiting verification"), membership_payments.get("PENDING_VERIFICATION"),
         "administration:membership_payments", ""),
        (_("Advertisers awaiting review"), advertising.get("organizations", {}).get("PENDING"), "administration:advertisers", ""),
        (_("Campaigns awaiting review"), advertising.get("campaigns", {}).get("SUBMITTED"), "administration:campaigns", ""),
        (_("Stuck video uploads"), videos.get("stuck"), "administration:videos", ""),
        (_("Viewing sessions awaiting review"), viewing.get("pending_review"), "administration:viewing", "?review=1"),
        (_("Demo reward policies awaiting approval"), rewards.get("draft_policies"), "administration:reward_policies", ""),
        (_("Sandbox payment exceptions"), payments.get("open_exceptions"), "administration:payment_exceptions", ""),
        (_("Sandbox refunds awaiting approval"), payments.get("pending_refunds"), "administration:payments", ""),
        (_("Sandbox disputes"), payments.get("open_disputes"), "administration:payments", ""),
        (_("Wallet reconciliation findings"), wallets.get("open_findings"), "administration:wallet_reconciliation", ""),
        (_("Demo withdrawals in review"), withdrawals.get("review_queue"), "administration:withdrawals", ""),
        (_("Demo withdrawals with unknown payout status"), withdrawals.get("uncertain"), "administration:withdrawals", "?status=DEMO_UNCERTAIN"),
        (_("Demo withdrawal exceptions"), withdrawals.get("open_exceptions"), "administration:withdrawal_exceptions", ""),
    )
    return [{"label": label, "count": count, "url_name": url_name, "query": query}
            for label, count, url_name, query in rows if count]


def _members():
    from apps.memberships.models import Subscription

    User = get_user_model()
    now = timezone.now()
    members = User.objects.filter(role=User.Role.MEMBER)
    totals = members.aggregate(total=Count("id"),
                               active=Count("id", filter=Q(status=User.Status.ACTIVE)),
                               verified=Count("id", filter=Q(email_verified=True)),
                               restricted=Count("id", filter=Q(status__in=[User.Status.SUSPENDED,
                                                                           User.Status.DISABLED])))
    statuses = _counts(Subscription.objects.all(), "status")
    current = Subscription.objects.filter(status=Subscription.Status.ACTIVE, starts_at__lte=now,
                                          expires_at__gt=now).count()
    return {**totals, "memberships": statuses, "current_memberships": current}


def _membership_payments():
    """PAY-01: real (manual Airtel Lipa) membership payments by status."""
    from apps.payments.models import ManualMembershipPayment

    return _counts(ManualMembershipPayment.objects.all(), "status")


def _advertising(can):
    from apps.advertising.models import Campaign, Organization

    data = {}
    if can["advertisers"]:
        data["organizations"] = _counts(Organization.objects.all(), "verification_status")
    if can["campaigns"]:
        data["campaigns"] = _counts(Campaign.objects.all(), "status")
        data["demo_budget_running"] = Campaign.objects.filter(
            status__in=[Campaign.Status.SCHEDULED, Campaign.Status.ACTIVE, Campaign.Status.PAUSED]).aggregate(
            t=Sum("demo_budget_amount", default=0))["t"]
        data["funded"] = Campaign.objects.exclude(funding_status=Campaign.Funding.UNFUNDED).count()  # Always 0.
    return data


def _videos():
    from apps.videos.models import Video

    stale = timezone.now() - timedelta(hours=settings.VIDEO_UPLOAD_STALE_HOURS)
    return {"publication": _counts(Video.objects.all(), "publication_status"),
            "processing": _counts(Video.objects.all(), "processing_status"),
            "stuck": Video.objects.filter(processing_status__in=[Video.Processing.UPLOADING,
                                                                 Video.Processing.PROCESSING],
                                          updated_at__lt=stale).count(),
            "provider": settings.VIDEO_PROVIDER}


def _viewing():
    from apps.viewing.models import WatchSession

    idle = timezone.now() - timedelta(seconds=settings.VIEWING_IDLE_SECONDS * 2)
    return {"outcomes": _counts(WatchSession.objects.all(), "outcome"),
            "pending_review": WatchSession.objects.filter(review_status=WatchSession.Review.PENDING).count(),
            "revoked": WatchSession.objects.filter(outcome=WatchSession.Outcome.REJECTED,
                                                   review_status=WatchSession.Review.REJECTED).count(),
            "stuck": WatchSession.objects.filter(state=WatchSession.State.IN_PROGRESS,
                                                 last_event_at__lt=idle).count()}


def _rewards():
    from apps.rewards.models import CampaignRewardBudget, DemoReward, RewardPolicy

    S = DemoReward.Status
    totals = DemoReward.objects.aggregate(
        qualified=Count("id", filter=Q(status=S.QUALIFIED)), reversed=Count("id", filter=Q(status=S.REVERSED)),
        rejected=Count("id", filter=Q(status=S.REJECTED)),
        simulated_total=Sum("amount", filter=Q(status=S.QUALIFIED), default=0))
    budgets = list(CampaignRewardBudget.objects.select_related("campaign").filter(demo_consumed__gt=0)
                   .order_by("-demo_consumed")[:5])
    return {**totals, "policies": _counts(RewardPolicy.objects.all(), "status"),
            "draft_policies": RewardPolicy.objects.filter(status=RewardPolicy.Status.DRAFT).count(),
            "budgets": [{"campaign": b.campaign.name, "consumed": b.demo_consumed,
                         "allocated": b.campaign.demo_budget_amount} for b in budgets]}


def _payments():
    from apps.payments.models import (
        MembershipActivationPreview,
        PaymentDispute,
        PaymentIntent,
        ReconciliationException,
        RefundRequest,
    )

    return {"statuses": _counts(PaymentIntent.objects.all(), "status"),
            "by_purpose": _counts(PaymentIntent.objects.all(), "purpose"),
            "open_exceptions": ReconciliationException.objects.filter(status="OPEN").count(),
            "pending_refunds": RefundRequest.objects.filter(status="REQUESTED").count(),
            "open_disputes": PaymentDispute.objects.filter(status="OPEN").count(),
            "previews": MembershipActivationPreview.objects.count()}


def _wallets():
    from apps.wallets.models import Wallet, WalletDiscrepancy

    findings = WalletDiscrepancy.objects.filter(status="OPEN")
    return {"total": Wallet.objects.count(),
            "restricted": Wallet.objects.filter(status=Wallet.Status.RESTRICTED).count(),
            "open_findings": findings.count(),
            "unexpected_balance": findings.filter(code="unexpected_balance").count()}


def _withdrawals():
    from apps.withdrawals.models import DemoWithdrawal, WithdrawalException

    D = DemoWithdrawal.Status
    return {"statuses": _counts(DemoWithdrawal.objects.all(), "status"),
            "review_queue": DemoWithdrawal.objects.filter(status__in=[D.REQUESTED, D.UNDER_REVIEW]).count(),
            "uncertain": DemoWithdrawal.objects.filter(status=D.UNCERTAIN).count(),
            "open_exceptions": WithdrawalException.objects.filter(status="OPEN").count()}


def _jobs():
    from apps.core.jobs.services import queue_health

    jobs = queue_health()
    if jobs["heartbeat_age_seconds"] is None:
        jobs["status"] = NOT_CHECKED  # No worker has ever reported: unknown, not broken.
    elif jobs["problems"] or jobs["failed_last_24h"]:
        jobs["status"] = ATTENTION
    else:
        jobs["status"] = HEALTHY
    jobs["status_label"] = STATUS_LABELS[jobs["status"]]
    return jobs


# --- Operational health ------------------------------------------------------------------------


def health(user):
    """Checks this staff member may see. Uses stored data only: a configured external
    service is reported "Not checked", never "Healthy"."""
    can, sections = overview(user)
    checks = []
    if "jobs" in sections:
        jobs = sections["jobs"]
        details = {NOT_CHECKED: "No worker heartbeat recorded yet.", HEALTHY: "Worker heartbeat is fresh.",
                   ATTENTION: ", ".join(str(code_label(code)) for code in jobs["problems"])
                   or f"{jobs['failed_last_24h']} failed in 24 h"}
        checks.append(HealthCheck("jobs", "Background jobs", jobs["status"], details[jobs["status"]]))
        checks.append(_email_check())
    if "videos" in sections:
        checks.append(_video_check(sections["videos"]))
    if "viewing" in sections:
        v = sections["viewing"]
        problems = v["stuck"] + v["pending_review"]
        checks.append(HealthCheck("viewing", "Viewing sessions", ATTENTION if problems else HEALTHY,
                                  f"{v['pending_review']} awaiting review, {v['stuck']} idle sessions not yet closed.",
                                  "administration:viewing"))
    if "payments" in sections:
        p = sections["payments"]
        problems = p["open_exceptions"] + p["pending_refunds"] + p["open_disputes"]
        checks.append(HealthCheck("payments", "Sandbox payments and reconciliation",
                                  ATTENTION if problems else HEALTHY,
                                  f"{p['open_exceptions']} exceptions, {p['pending_refunds']} refunds awaiting a "
                                  f"second approver, {p['open_disputes']} disputes.", "administration:payment_exceptions"))
    elif can["operations"]:
        checks.append(HealthCheck("payments", "Sandbox payments", NOT_CONFIGURED, "Payments are disabled here."))
    if "wallets" in sections:
        w = sections["wallets"]
        checks.append(HealthCheck("wallets", "Wallet integrity", ATTENTION if w["open_findings"] else HEALTHY,
                                  f"{w['open_findings']} open reconciliation findings "
                                  f"({w['unexpected_balance']} unexpected balances).",
                                  "administration:wallet_reconciliation"))
    if "withdrawals" in sections:
        x = sections["withdrawals"]
        problems = x["uncertain"] + x["open_exceptions"]
        checks.append(HealthCheck("withdrawals", "Demo payouts", ATTENTION if problems else HEALTHY,
                                  f"{x['uncertain']} with unknown status, {x['open_exceptions']} exceptions.",
                                  "administration:withdrawal_exceptions"))
    if "advertising" in sections and "organizations" in sections["advertising"]:
        pending = sections["advertising"]["organizations"].get("PENDING", 0)
        submitted = sections["advertising"].get("campaigns", {}).get("SUBMITTED", 0)
        checks.append(HealthCheck("review_queues", "Advertiser and campaign reviews",
                                  ATTENTION if pending or submitted else HEALTHY,
                                  f"{pending} advertisers and {submitted} campaigns awaiting review.",
                                  "administration:advertisers"))
    if "rewards" in sections:
        drafts = sections["rewards"]["draft_policies"]
        checks.append(HealthCheck("reward_policies", "Demo reward policies", ATTENTION if drafts else HEALTHY,
                                  f"{drafts} drafts awaiting a second approver.", "administration:reward_policies"))
    if can["operations"]:
        findings = database_role_findings()
        checks.append(HealthCheck(
            "database_roles", "Database privilege separation", ATTENTION if findings else HEALTHY,
            ("; ".join(findings) + ". The application's own login could switch off the database protections; "
             "apply scripts/database/roles (audit CC-005).") if findings else
            "The application's login owns no protected table and cannot change audit or ledger history."))
    return checks


# The search_path every ClipCash database function runs with (migration core.0004).
PINNED_SEARCH_PATH = "search_path=pg_catalog, public, pg_temp"

# Tables whose triggers and constraints carry the financial and audit guarantees.
PROTECTED_TABLES = ("audit_auditevent", "wallets_journalentry", "wallets_journalline", "wallets_ledgeraccount",
                    "wallets_wallet", "wallets_wallettransaction", "withdrawals_withdrawalrequest",
                    "advertising_campaign", "memberships_subscription")


def database_role_findings():
    """Why the runtime database login is not separated from the owner ([] = separated).
    Read-only catalog queries about the current login."""
    with connection.cursor() as cursor:
        cursor.execute("SELECT rolsuper FROM pg_roles WHERE rolname = current_user")
        (superuser,) = cursor.fetchone()
        cursor.execute("SELECT count(*) FROM pg_tables WHERE schemaname = 'public' AND tablename = ANY(%s) "
                       "AND pg_has_role(current_user, tableowner, 'USAGE')", [list(PROTECTED_TABLES)])
        (owned,) = cursor.fetchone()
        cursor.execute("SELECT has_table_privilege('public.audit_auditevent', 'UPDATE,DELETE,TRUNCATE'), "
                       "has_table_privilege('public.wallets_journalline', 'UPDATE,DELETE,TRUNCATE')")
        audit_writable, ledger_writable = cursor.fetchone()
        # RG-03: objects that could shadow protected tables, and guard functions that
        # would resolve names through the caller's search_path (migration core.0004).
        cursor.execute("SELECT has_database_privilege(current_database(), 'CREATE') "
                       "OR has_database_privilege(current_database(), 'TEMPORARY')")
        (can_shadow,) = cursor.fetchone()
        cursor.execute("SELECT count(*) FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace "
                       "WHERE n.nspname = 'public' "
                       "AND NOT EXISTS (SELECT 1 FROM pg_depend d WHERE d.objid = p.oid AND d.deptype = 'e') "
                       "AND NOT coalesce(p.proconfig, '{}') @> ARRAY[%s]", [PINNED_SEARCH_PATH])
        (unpinned,) = cursor.fetchone()
    findings = []
    if superuser:
        findings.append("it connects as a database superuser")
    if owned:
        findings.append(f"it owns (or acts as the owner of) {owned} of {len(PROTECTED_TABLES)} protected tables")
    if audit_writable:
        findings.append("it may update, delete or truncate audit events")
    if ledger_writable:
        findings.append("it may update, delete or truncate ledger lines")
    if can_shadow:
        findings.append("it may create schemas or temporary tables in the database")
    if unpinned:
        findings.append(f"{unpinned} database guard functions do not pin their search_path (apply migrations)")
    return findings


def client_ip_diagnostics(request):
    """How this request's client address was resolved, for verifying
    DJANGO_TRUSTED_PROXY_COUNT on the real host (Super Administrators only: it shows
    the viewer's own addresses and the proxies' addresses)."""
    from apps.accounts.throttling import peer_is_trusted, resolve_client_ip, trusted_proxy_networks

    forwarded = [p.strip() for p in request.META.get("HTTP_X_FORWARDED_FOR", "").split(",") if p.strip()]
    address, resolved = resolve_client_ip(request)
    remote_addr = request.META.get("REMOTE_ADDR", "")
    return {"remote_addr": remote_addr, "forwarded": forwarded,
            "trusted_proxies": settings.ACCOUNTS_TRUSTED_PROXY_COUNT, "address": address, "resolved": resolved,
            "header": settings.ACCOUNTS_CLIENT_IP_HEADER, "networks": len(trusted_proxy_networks()),
            "peer_trusted": peer_is_trusted(remote_addr)}


def _email_check():
    from apps.notifications.models import OutboundEmail

    backend = settings.EMAIL_BACKEND.rsplit(".", 1)[-1]
    since = timezone.now() - timedelta(hours=24)
    failed = OutboundEmail.objects.filter(status=OutboundEmail.Status.FAILED, created_at__gte=since).count()
    waiting = OutboundEmail.objects.filter(status=OutboundEmail.Status.PENDING,
                                           created_at__lt=timezone.now() - timedelta(hours=1)).count()
    if "console" in settings.EMAIL_BACKEND or "locmem" in settings.EMAIL_BACKEND:
        return HealthCheck("email", "Email delivery", NOT_CONFIGURED,
                           f"Development backend ({backend}): emails are not delivered to real inboxes.")
    if failed or waiting:
        return HealthCheck("email", "Email delivery", ATTENTION,
                           f"{failed} failed in 24 h, {waiting} waiting longer than an hour.")
    return HealthCheck("email", "Email delivery", NOT_CHECKED,
                       "Outbox has no failures; the mail server itself is not tested from this page.")


def _video_check(videos):
    if videos["provider"] == "demo":
        return HealthCheck("video_provider", "Video provider", NOT_CONFIGURED,
                           "Demo provider: no Bunny Stream connection.", "administration:videos")
    required = ("BUNNY_STREAM_LIBRARY_ID", "BUNNY_STREAM_API_KEY", "BUNNY_STREAM_CDN_HOSTNAME",
                "BUNNY_STREAM_TOKEN_SECURITY_KEY")
    missing = [name for name in required if not getattr(settings, name, "")]
    if missing:
        return HealthCheck("video_provider", "Video provider", NOT_CONFIGURED,
                           f"{len(missing)} Bunny Stream setting(s) missing.", "administration:videos")
    failed = videos["processing"].get("FAILED", 0)
    if videos["stuck"] or failed:
        return HealthCheck("video_provider", "Video provider", ATTENTION,
                           f"{videos['stuck']} uploads/processing stuck, {failed} failed.", "administration:videos")
    return HealthCheck("video_provider", "Video provider", NOT_CHECKED,
                       "Configured; Bunny Stream is not contacted from this page.", "administration:videos")


# --- Members ----------------------------------------------------------------------------------


def member_directory(query=""):
    from apps.memberships.models import Subscription
    from apps.wallets.models import Wallet

    User = get_user_model()
    latest = Subscription.objects.filter(member=OuterRef("pk")).order_by("-created_at")
    qs = (User.objects.filter(role=User.Role.MEMBER)
          .annotate(membership_status=Subquery(latest.values("status")[:1]),
                    membership_plan=Subquery(latest.values("plan_name")[:1]),
                    wallet_status=Subquery(Wallet.objects.filter(member=OuterRef("pk")).values("status")[:1]))
          .order_by("-date_joined"))
    query = (query or "").strip()[:254]
    if query:
        qs = qs.filter(email__icontains=query)
    return qs


# --- Security and audit -----------------------------------------------------------------------


def security_summary(hours=24, limit=20):
    """Counts and a short recent list, time-bounded on indexed columns. No metadata."""
    from apps.audit.models import AuditEvent

    since = timezone.now() - timedelta(hours=hours)
    recent = AuditEvent.objects.filter(occurred_at__gte=since)
    by_outcome = _counts(recent, "outcome")
    denied_by_category = dict(recent.filter(outcome="DENIED").values_list("category").annotate(n=Count("id")))
    events = list(recent.order_by("-occurred_at").values("occurred_at", "category", "action", "outcome",
                                                          "actor_email")[:limit])
    staff_actions = list(recent.filter(actor_role__in=["SUPER_ADMIN", "CONTENT_MANAGER"])
                         .order_by("-occurred_at").values("occurred_at", "action", "actor_email")[:limit])
    financial_denials = recent.filter(outcome="DENIED", category__in=["PAYMENTS", "WALLETS", "WITHDRAWALS",
                                                                       "REWARDS", "LEDGER"]).count()
    return {"hours": hours, "by_outcome": by_outcome, "denied_by_category": denied_by_category, "events": events,
            "staff_actions": staff_actions, "financial_denials": financial_denials}
