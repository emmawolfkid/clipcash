"""
Reports (Phase 15-C): read-time aggregates over the authoritative records.

Rules
-----
* Read-only. No new tables, balances or postings. Figures are counts and sums of
  stored records in a period, or the owning service's own output ("as of now").
* Money-like figures stay separate. Each section names one funds type (FUNDS) and
  nothing ever adds two of them together: there is no grand total.
* Personal data minimised. Aggregates only; no member or staff identity appears in
  any report or export. Organization and campaign names are business data.
* Privacy (owner decision D12). Advertisers always get advertising.report_figure's
  "fewer than N"; staff see exact counts on screen, and per-campaign figures are
  masked the same way in CSV exports, because files travel.
"""

import csv
import io
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from django.conf import settings
from django.contrib.auth import get_user_model
from django.db.models import Count, Q, Sum
from django.utils import timezone
from django.utils.encoding import force_str
from django.utils.translation import gettext_lazy as _

from apps.accounts.throttling import Throttle
from apps.administration import summary as admin_summary
from apps.audit.services import DENIED, SUCCESS, record_security_event
from apps.core.services import get_site_configuration

MAX_PERIOD_DAYS = 366
DEFAULT_PERIOD_DAYS = 30
MAX_ROWS = 100  # Per-campaign and per-account tables; pages say when they are cut.
EXPORTS_PER_HOUR = Throttle("report-export", limit=20, window_seconds=60 * 60)

FUNDS = {
    "real": _("Real money (locked, 0)"),
    "sandbox": _("Sandbox (test data, not money)"),
    "simulated": _("Simulated demo rewards (not money)"),
    "demo": _("Demo withdrawals (no money moves)"),
    "budget": _("Demo budget (not money)"),
}
PERIOD, NOW = "period", "now"


class PeriodError(ValueError):
    pass


@dataclass
class Period:
    first: date
    last: date
    start: datetime
    end: datetime  # Exclusive.

    def of(self, field_name):
        return {f"{field_name}__gte": self.start, f"{field_name}__lt": self.end}


@dataclass
class Section:
    title: object
    columns: list
    rows: list
    funds: str = ""  # A FUNDS key, or "" when the section has no money-like figures.
    scope: str = PERIOD
    truncated: bool = False
    label_columns: int = 1  # Leading columns that name the row, not figures.

    @property
    def funds_label(self):
        return FUNDS.get(self.funds, "")

    @property
    def as_of_now(self):
        return self.scope == NOW


@dataclass
class Report:
    slug: str
    title: object
    sections: list = field(default_factory=list)


def period_from(first="", last=""):
    """The period between two ISO dates, inclusive, in the site's time zone. Empty means
    the last DEFAULT_PERIOD_DAYS days. Raises PeriodError on anything else unusable."""
    zone = ZoneInfo(get_site_configuration().default_timezone)
    today = timezone.now().astimezone(zone).date()
    try:
        last_day = date.fromisoformat(last) if last else today
        first_day = date.fromisoformat(first) if first else last_day - timedelta(days=DEFAULT_PERIOD_DAYS - 1)
    except ValueError:
        raise PeriodError(_("Enter dates as YYYY-MM-DD.")) from None
    if first_day > last_day:
        raise PeriodError(_("The start date must not be after the end date."))
    if (last_day - first_day).days >= MAX_PERIOD_DAYS:
        raise PeriodError(_("Choose a period of at most %(days)d days.") % {"days": MAX_PERIOD_DAYS})
    return Period(first_day, last_day, datetime.combine(first_day, time.min, zone),
                  datetime.combine(last_day + timedelta(days=1), time.min, zone))


# --- Building blocks -------------------------------------------------------------------------


def _by_choice(queryset, field_name, choices, *, amount=None):
    """One row per choice (zeros included): [label, count] or [label, count, sum of amount]."""
    figures = {"n": Count("id")}
    if amount:
        figures["total"] = Sum(amount, default=0)
    found = {row[field_name]: row for row in queryset.order_by().values(field_name).annotate(**figures)}
    rows = []
    for value, label in choices:
        row = found.get(value, {})
        rows.append([label, row.get("n", 0)] + ([row.get("total", 0)] if amount else []))
    return rows


def _by_value(queryset, field_name, limit=MAX_ROWS):
    """[value, count] for free-form values (job types, email kinds), largest first."""
    rows = queryset.order_by().values_list(field_name).annotate(n=Count("id")).order_by("-n", field_name)[:limit]
    return [[value, n] for value, n in rows]


def _figure(value, *, exact):
    from apps.advertising.services import report_figure

    figure = report_figure(value or 0, for_advertiser=not exact)
    return figure["display"] if figure["value"] is None else figure["value"]


COUNT = _("Count")

# --- The eight reports -----------------------------------------------------------------------


def _members(period, exact):
    from apps.memberships.models import Subscription

    User = get_user_model()
    members = User.objects.filter(role=User.Role.MEMBER, **period.of("date_joined"))
    totals = members.aggregate(
        registered=Count("id"), active=Count("id", filter=Q(status=User.Status.ACTIVE)),
        verified=Count("id", filter=Q(email_verified=True)),
        restricted=Count("id", filter=Q(status__in=[User.Status.SUSPENDED, User.Status.DISABLED])))
    now = admin_summary._members()
    return [
        Section(_("Members who registered"), [_("Measure"), COUNT], [
            [_("Registered"), totals["registered"]], [_("Active today"), totals["active"]],
            [_("Email confirmed"), totals["verified"]], [_("Suspended or disabled"), totals["restricted"]]]),
        Section(_("Memberships created, by status"), [_("Status"), COUNT],
                _by_choice(Subscription.objects.filter(**period.of("created_at")), "status",
                           Subscription.Status.choices)),
        Section(_("Membership activations, by source"), [_("Source"), COUNT],
                _by_choice(Subscription.objects.filter(**period.of("activated_at")), "activation_source",
                           Subscription._meta.get_field("activation_source").choices)),
        Section(_("All members today"), [_("Measure"), COUNT], [
            [_("Members"), now["total"]], [_("Active"), now["active"]], [_("Email confirmed"), now["verified"]],
            [_("Current memberships"), now["current_memberships"]]], scope=NOW),
    ]


def _campaign_viewing(period, exact):
    from apps.viewing.models import WatchSession
    from apps.viewing.services import PERFORMANCE_FIGURES

    rows = list(WatchSession.objects.filter(**period.of("created_at"))
                .values("campaign__name", "campaign__organization__display_name")
                .annotate(**PERFORMANCE_FIGURES).order_by("-verified", "campaign__name")[:MAX_ROWS + 1])
    keys = ("sessions", "verified", "viewers", "in_review", "not_verified")
    return Section(
        _("Viewing by campaign"),
        [_("Campaign"), _("Advertiser"), _("Sessions"), _("Verified views"), _("Unique viewers"), _("In review"),
         _("Not verified")],
        [[row["campaign__name"], row["campaign__organization__display_name"],
          *[_figure(row[key], exact=exact) for key in keys]] for row in rows[:MAX_ROWS]],
        truncated=len(rows) > MAX_ROWS, label_columns=2)


def _advertising(period, exact):
    from apps.advertising.models import Campaign, Organization
    from apps.advertising.services import campaign_summary
    from apps.viewing.models import WatchSession

    campaigns = Campaign.objects.filter(**period.of("created_at"))
    budgets = campaign_summary(campaigns)
    return [
        Section(_("Advertisers who applied, by verification status"), [_("Status"), COUNT],
                _by_choice(Organization.objects.filter(**period.of("created_at")), "verification_status",
                           Organization.Verification.choices)),
        Section(_("Campaigns created, by status"), [_("Status"), COUNT],
                _by_choice(campaigns, "status", Campaign.Status.choices)),
        Section(_("Viewing sessions, by outcome"), [_("Outcome"), COUNT],
                _by_choice(WatchSession.objects.filter(**period.of("created_at")), "outcome",
                           WatchSession.Outcome.choices)),
        _campaign_viewing(period, exact),
        Section(_("Demo budget of campaigns created (TZS)"), [_("Measure"), _("Demo budget (TZS)")], [
            [_("In approved campaigns"), budgets["demo_budget_running"]],
            [_("All except cancelled or archived"), budgets["demo_budget_all"]]], funds="budget"),
        Section(_("Real campaign funding"), [_("Measure"), COUNT], [
            [_("Campaigns with real funding"), Campaign.objects.exclude(funding_status=Campaign.Funding.UNFUNDED).count()]],
            funds="real", scope=NOW),
    ]


def _reward_figures():
    from apps.rewards.models import DemoReward

    S = DemoReward.Status
    return {"qualified": Count("id", filter=Q(status=S.QUALIFIED)), "reversed": Count("id", filter=Q(status=S.REVERSED)),
            "rejected": Count("id", filter=Q(status=S.REJECTED)),
            "demo_consumed": Sum("amount", filter=Q(status=S.QUALIFIED), default=0)}


def reward_totals(period):
    from apps.rewards.models import DemoReward

    return DemoReward.objects.filter(**period.of("decided_at")).aggregate(**_reward_figures())


def _rewards(period, exact):
    from apps.rewards.models import DemoReward, RewardPolicy

    rewards = DemoReward.objects.filter(**period.of("decided_at"))
    totals = reward_totals(period)
    by_campaign = list(rewards.values("campaign__name").annotate(**_reward_figures())
                       .order_by("campaign__name")[:MAX_ROWS + 1])
    rows = []
    for row in by_campaign[:MAX_ROWS]:
        qualified = _figure(row["qualified"], exact=exact)
        # The amount is qualified x reward: when the count is hidden, so is the amount.
        consumed = row["demo_consumed"] if isinstance(qualified, int) else qualified
        rows.append([row["campaign__name"], qualified, _figure(row["reversed"], exact=exact),
                     _figure(row["rejected"], exact=exact), consumed])
    return [
        Section(_("Demo reward decisions"), [_("Measure"), _("Value")], [
            [_("Qualified"), totals["qualified"]], [_("Reversed"), totals["reversed"]],
            [_("Rejected"), totals["rejected"]], [_("Simulated amount, qualified (TZS)"), totals["demo_consumed"]]],
            funds="simulated"),
        Section(_("Demo rewards by campaign"),
                [_("Campaign"), _("Qualified"), _("Reversed"), _("Rejected"), _("Simulated amount (TZS)")],
                rows, funds="simulated", truncated=len(by_campaign) > MAX_ROWS),
        Section(_("Reward policies today, by status"), [_("Status"), COUNT],
                _by_choice(RewardPolicy.objects.all(), "status", RewardPolicy.Status.choices), scope=NOW),
    ]


def _payments(period, exact):
    from apps.payments import services as payments
    from apps.payments.models import PaymentDispute, PaymentIntent, ReconciliationException, RefundRequest

    intents = PaymentIntent.objects.filter(**period.of("created_at"))
    exceptions = ReconciliationException.objects.filter(**period.of("created_at"))
    amount_title = _("Sandbox amount (TZS)")
    balances = list(payments.sandbox_balances().items())
    wallets = admin_summary._wallets()
    return [
        Section(_("Sandbox payments, by status"), [_("Status"), COUNT, amount_title],
                _by_choice(intents, "status", PaymentIntent.Status.choices, amount="amount"), funds="sandbox"),
        Section(_("Sandbox payments, by purpose"), [_("Purpose"), COUNT, amount_title],
                _by_choice(intents, "purpose", PaymentIntent.Purpose.choices, amount="amount"), funds="sandbox"),
        Section(_("Reconciliation exceptions, by reason"), [_("Reason"), COUNT],
                _by_choice(exceptions, "reason", ReconciliationException.Reason.choices), funds="sandbox"),
        Section(_("Refunds and disputes"), [_("Measure"), COUNT], [
            [_("Refunds requested"), RefundRequest.objects.filter(**period.of("created_at")).count()],
            [_("Disputes opened"), PaymentDispute.objects.filter(**period.of("created_at")).count()]], funds="sandbox"),
        Section(_("Sandbox journal balances (debit minus credit)"), [_("Account"), _("Scope"), amount_title],
                [[account, scope or _("platform"), balance] for (account, scope), balance in balances[:MAX_ROWS]],
                funds="sandbox", scope=NOW, truncated=len(balances) > MAX_ROWS, label_columns=2),
        Section(_("Real wallets"), [_("Measure"), COUNT], [
            [_("Wallets"), wallets["total"]], [_("Open reconciliation findings"), wallets["open_findings"]],
            [_("Unexpected balances"), wallets["unexpected_balance"]]], funds="real", scope=NOW),
    ]


def _withdrawals(period, exact):
    from apps.withdrawals.models import DemoWithdrawal, WithdrawalException

    now = admin_summary._withdrawals()
    return [
        Section(_("Demo withdrawals, by status"), [_("Status"), COUNT, _("Demo amount (TZS)")],
                _by_choice(DemoWithdrawal.objects.filter(**period.of("created_at")), "status",
                           DemoWithdrawal.Status.choices, amount="simulated_amount"), funds="demo"),
        Section(_("Demo withdrawal exceptions, by code"), [_("Code"), COUNT],
                _by_value(WithdrawalException.objects.filter(**period.of("created_at")), "code"), funds="demo"),
        Section(_("Demo withdrawal queues today"), [_("Measure"), COUNT], [
            [_("Awaiting review"), now["review_queue"]], [_("Unknown result"), now["uncertain"]],
            [_("Open exceptions"), now["open_exceptions"]]], funds="demo", scope=NOW),
    ]


def _operations(period, exact):
    from apps.core.models import Job
    from apps.notifications.models import OutboundEmail

    jobs = Job.objects.filter(**period.of("created_at"))
    emails = OutboundEmail.objects.filter(**period.of("created_at"))
    health = admin_summary._jobs()
    return [
        Section(_("Background jobs, by status"), [_("Status"), COUNT], _by_choice(jobs, "status", Job.Status.choices)),
        Section(_("Background jobs, by type"), [_("Job type"), COUNT], _by_value(jobs, "job_type")),
        Section(_("Emails, by status"), [_("Status"), COUNT],
                _by_choice(emails, "status", OutboundEmail.Status.choices)),
        Section(_("Emails, by kind"), [_("Kind"), COUNT], _by_value(emails, "kind")),
        Section(_("Job queue today"), [_("Measure"), _("Value")], [
            [_("Status"), force_str(health["status_label"])], [_("Failed in the last 24 hours"), health["failed_last_24h"]],
            [_("Problems"), ", ".join(health["problems"]) or "—"]], scope=NOW),
    ]


def _audit(period, exact):
    from apps.audit.events import Category
    from apps.audit.models import AuditEvent

    events = AuditEvent.objects.filter(**period.of("occurred_at"))
    denied = events.filter(outcome=AuditEvent.Outcome.DENIED)
    financial = denied.filter(category__in=[Category.PAYMENTS, Category.WALLETS, Category.WITHDRAWALS,
                                            Category.REWARDS, Category.LEDGER]).count()
    return [
        Section(_("Audit events, by outcome"), [_("Outcome"), COUNT],
                _by_choice(events, "outcome", AuditEvent.Outcome.choices)),
        Section(_("Audit events, by category"), [_("Category"), COUNT], _by_choice(events, "category", Category.choices)),
        Section(_("Denied actions, by category"), [_("Category"), COUNT], _by_choice(denied, "category", Category.choices)),
        Section(_("Denied financial actions"), [_("Measure"), COUNT], [[_("Denied financial actions"), financial]]),
        Section(_("Most denied actions"), [_("Action"), COUNT], _by_value(denied, "action", limit=20)),
    ]


def _support(period, exact):
    from apps.support.models import Ticket, TicketMessage
    from apps.support.services import OPEN_STATES

    tickets = Ticket.objects.filter(**period.of("created_at"))
    messages = TicketMessage.objects.filter(**period.of("created_at"))
    open_tickets = Ticket.objects.filter(status__in=OPEN_STATES)
    statuses = [(value, _("Waiting for customer") if value == Ticket.Status.WAITING else label)
                for value, label in Ticket.Status.choices]
    return [
        Section(_("Tickets opened, by status"), [_("Status"), COUNT], _by_choice(tickets, "status", statuses)),
        Section(_("Tickets opened, by category"), [_("Category"), COUNT],
                _by_choice(tickets, "category", Ticket.Category.choices)),
        Section(_("Tickets opened, by priority"), [_("Priority"), COUNT],
                _by_choice(tickets, "priority", Ticket.Priority.choices)),
        Section(_("Activity"), [_("Measure"), COUNT], [
            [_("Tickets resolved"), Ticket.objects.filter(**period.of("resolved_at")).count()],
            [_("Tickets closed"), Ticket.objects.filter(**period.of("closed_at")).count()],
            [_("Customer messages"), messages.filter(from_staff=False).count()],
            [_("Staff replies"), messages.filter(from_staff=True, internal=False).count()],
            [_("Internal notes"), messages.filter(internal=True).count()]]),
        Section(_("Open tickets today"), [_("Measure"), COUNT], [
            [_("Open"), open_tickets.count()], [_("Unassigned"), open_tickets.filter(assignee__isnull=True).count()]],
            scope=NOW),
    ]


@dataclass(frozen=True)
class ReportType:
    slug: str
    title: object
    summary: object
    permission: str
    builder: object


REPORT_TYPES = {report.slug: report for report in (
    ReportType("members", _("Members and memberships"), _("Registrations, memberships and activations."),
               "reports.view_members_report", _members),
    ReportType("advertising", _("Advertisers and campaigns"), _("Applications, campaigns and verified viewing."),
               "reports.view_advertising_report", _advertising),
    ReportType("rewards", _("Simulated rewards"), _("Demo reward decisions. Simulated, never money."),
               "reports.view_rewards_report", _rewards),
    ReportType("payments", _("Sandbox payments"), _("Sandbox payments and reconciliation. Test data, never money."),
               "reports.view_payments_report", _payments),
    ReportType("withdrawals", _("Demo withdrawals"), _("Demo withdrawal requests and exceptions. No money moves."),
               "reports.view_withdrawals_report", _withdrawals),
    ReportType("operations", _("Operations"), _("Background jobs and email delivery."),
               "reports.view_operations_report", _operations),
    ReportType("audit", _("Audit and security"), _("Audit events and denied actions, without identities."),
               "reports.view_audit_report", _audit),
    ReportType("support", _("Customer support"), _("Tickets and support activity."),
               "reports.view_support_report", _support),
)}


# --- Access ----------------------------------------------------------------------------------


def _staff_with(user, permission):
    return bool(user is not None and getattr(user, "is_authenticated", False) and user.is_active
                and user.is_staff and user.has_perm(permission))


def can_view(user, slug):
    report = REPORT_TYPES.get(slug)
    return report is not None and _staff_with(user, report.permission)


def visible_reports(user):
    return [report for report in REPORT_TYPES.values() if _staff_with(user, report.permission)]


def can_export(user, slug):
    return can_view(user, slug) and _staff_with(user, "reports.export_reports")


def deny(user, action, slug, reason="permission_missing"):
    record_security_event("reports.action.denied", outcome=DENIED, actor=user,
                          metadata={"action": action, "report": slug[:40], "reason": reason})


def build(slug, period, *, exact=True):
    report = REPORT_TYPES[slug]
    return Report(slug, report.title, report.builder(period, exact))


# --- CSV export ------------------------------------------------------------------------------


def _cell(value):
    """A CSV cell. Text that a spreadsheet would run as a formula is neutralised."""
    if isinstance(value, (int, float)):
        return value
    text = force_str(value)
    return f"'{text}" if text[:1] in ("=", "+", "-", "@", "\t", "\r") else text


def export_csv(user, slug, period):
    """The report as long-format CSV (one figure per line, each with its section and funds
    type, so figures of different kinds cannot be mistaken for one column), audited."""
    report = build(slug, period, exact=False)
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\r\n")
    writer.writerow([_cell(title) for title in (_("Section"), _("Funds type"), _("Period start"), _("Period end"),
                                                _("As of"), _("Row"), _("Measure"), _("Value"))])
    rows = 0
    for section in report.sections:
        scope = _("today") if section.as_of_now else _("period")
        for row in section.rows:
            label = " / ".join(force_str(value) for value in row[:section.label_columns])
            for title, value in zip(section.columns[section.label_columns:], row[section.label_columns:]):
                writer.writerow([_cell(section.title), _cell(section.funds_label), period.first.isoformat(),
                                 period.last.isoformat(), _cell(scope), _cell(label), _cell(title), _cell(value)])
                rows += 1
    record_security_event("reports.exported", outcome=SUCCESS, actor=user, metadata={
        "report": slug, "start": period.first.isoformat(), "end": period.last.isoformat(), "rows": rows})
    return "﻿" + buffer.getvalue()  # BOM: spreadsheets then read names as UTF-8.


# --- Advertiser report -----------------------------------------------------------------------


def advertiser_report(organization, period, campaigns):
    """One organization's campaigns in a period, always with D12 masking. ``campaigns``
    is the page of that organization's campaigns to detail."""
    from apps.advertising.services import report_figure
    from apps.rewards.models import DemoReward
    from apps.viewing.models import WatchSession
    from apps.viewing.services import PERFORMANCE_FIGURES

    sessions = WatchSession.objects.filter(campaign__organization=organization, **period.of("created_at"))
    totals = sessions.aggregate(**PERFORMANCE_FIGURES)
    by_campaign = {row.pop("campaign_id"): row for row in
                   sessions.filter(campaign__in=[c.pk for c in campaigns]).values("campaign_id")
                   .annotate(**PERFORMANCE_FIGURES).order_by()}
    rewards = dict(DemoReward.objects.filter(campaign__in=[c.pk for c in campaigns], status=DemoReward.Status.QUALIFIED,
                                             **period.of("decided_at"))
                   .order_by().values_list("campaign_id").annotate(n=Count("id")))

    def masked(figures):
        return {key: report_figure(figures.get(key) or 0, for_advertiser=True)["display"]
                for key in ("sessions", "verified", "viewers", "in_review", "not_verified")}

    return {
        "totals": masked(totals),
        "rows": [{"campaign": campaign, **masked(by_campaign.get(campaign.pk, {})),
                  "qualified": report_figure(rewards.get(campaign.pk, 0), for_advertiser=True)["display"]}
                 for campaign in campaigns],
    }


def privacy_minimum():
    return settings.ADVERTISING_REPORT_MIN_COUNT
