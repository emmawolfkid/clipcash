"""
Staff analytics (Phase 15-D): trends and summary figures over the Reports period.

Same rules as apps.reports.services: read-only, aggregates only, no identities, no
new balances. A section needs the permission of the report it summarises, so analytics
grants nothing a staff member could not already read. Each money-like section names one
funds type and no figure is ever added across types: there is no grand total.

Charts are plain data with precomputed SVG geometry (strings, so localised number
formats cannot reach SVG attributes). No JavaScript, chart library or inline CSS.
"""

from dataclasses import dataclass, field
from datetime import date, timedelta

from django.contrib.auth import get_user_model
from django.db.models import Count, Q
from django.db.models.functions import TruncDay, TruncMonth, TruncWeek
from django.utils.formats import date_format
from django.utils.translation import gettext_lazy as _

from apps.administration import summary as admin_summary

from . import services
from .services import FUNDS, _by_choice

DAY, WEEK, MONTH = "day", "week", "month"
TRUNCATE = {DAY: TruncDay, WEEK: TruncWeek, MONTH: TruncMonth}
TOP_CAMPAIGNS = 5

# SVG geometry (viewBox units; the chart scales with its container).
WIDTH, TOP, PLOT_HEIGHT, LEFT = 640, 12, 160, 40
BASELINE = TOP + PLOT_HEIGHT


@dataclass
class Card:
    label: object
    value: object
    now: bool = False  # "As of today" rather than "in this period".


@dataclass
class Section:
    key: str
    title: object
    report: str  # The report holding the details.
    cards: list = field(default_factory=list)
    charts: list = field(default_factory=list)
    breakdowns: list = field(default_factory=list)
    table: dict = None
    funds: str = ""  # A FUNDS key, or "" when the section has no money-like figures.

    @property
    def funds_label(self):
        return FUNDS.get(self.funds, "")


# --- Trends ----------------------------------------------------------------------------------


def grain_for(period):
    days = (period.last - period.first).days + 1
    return DAY if days <= 45 else WEEK if days <= 200 else MONTH


def buckets_for(period, grain):
    """The start date of every bucket in the period, in order (zero-filled charts)."""
    if grain == DAY:
        return [period.first + timedelta(days=n) for n in range((period.last - period.first).days + 1)]
    if grain == WEEK:
        monday = period.first - timedelta(days=period.first.weekday())
        return [monday + timedelta(weeks=n) for n in range((period.last - monday).days // 7 + 1)]
    months, current = [], date(period.first.year, period.first.month, 1)
    while current <= period.last:
        months.append(current)
        current = date(current.year + current.month // 12, current.month % 12 + 1, 1)
    return months


def counts_by_bucket(queryset, field_name, period, grain):
    """{bucket start date: count} for records in the period. One query."""
    truncated = TRUNCATE[grain](field_name, tzinfo=period.start.tzinfo)
    rows = (queryset.filter(**period.of(field_name)).order_by().annotate(bucket=truncated)
            .values_list("bucket").annotate(n=Count("id")))
    return {bucket.date(): n for bucket, n in rows}


def _label(day, grain):
    return date_format(day, "M Y" if grain == MONTH else "j M")


def chart(title, series, period, grain):
    """``series`` is [(label, {bucket: count})], at most two. Returns SVG geometry plus the
    same figures as a table, so the chart is readable without seeing it."""
    buckets = buckets_for(period, grain)
    peak = max([1] + [count for _, counts in series for count in counts.values()])
    slot = (WIDTH - LEFT) / len(buckets)
    bar = slot * 0.8 / len(series)
    bars = []
    for index, (label, counts) in enumerate(series):
        for position, bucket in enumerate(buckets):
            value = counts.get(bucket, 0)
            if not value:
                continue
            height = max(value / peak * PLOT_HEIGHT, 1.5)  # Small values stay visible.
            bars.append({
                "x": f"{LEFT + position * slot + slot * 0.1 + index * bar:.2f}", "y": f"{BASELINE - height:.2f}",
                "width": f"{bar:.2f}", "height": f"{height:.2f}", "series": index + 1,
                "title": f"{label}, {_label(bucket, grain)}: {value}"})
    ticks = sorted({0, len(buckets) // 2, len(buckets) - 1})
    return {
        "title": title, "bars": bars, "peak": peak, "empty": not bars, "width": WIDTH,
        "height": BASELINE + 24, "left": LEFT, "right": WIDTH, "top": TOP, "baseline": BASELINE,
        "text_x": LEFT - 6, "peak_y": TOP + 5, "zero_y": BASELINE, "label_y": BASELINE + 18,
        "legend": [{"label": label, "series": index + 1} for index, (label, _counts) in enumerate(series)],
        "ticks": [{"x": f"{LEFT + tick * slot + slot / 2:.2f}", "label": _label(buckets[tick], grain),
                   "anchor": "end" if tick == len(buckets) - 1 and tick else "middle"} for tick in ticks],
        "columns": [label for label, _counts in series],
        "rows": [{"label": _label(bucket, grain), "values": [counts.get(bucket, 0) for _, counts in series]}
                 for bucket in buckets],
    }


def breakdown(title, rows):
    """Horizontal bars for [[label, count, ...]]: share of the largest value, zero rows kept out."""
    rows = [(label, count) for label, count, *_rest in rows if count]
    peak = max([count for _, count in rows], default=1)
    return {"title": title, "empty": not rows,
            "rows": [{"label": label, "value": count, "width": f"{count / peak * 100:.1f}"} for label, count in rows]}


# --- Sections --------------------------------------------------------------------------------


def _memberships(period, grain):
    from apps.memberships.models import Subscription

    User = get_user_model()
    members = User.objects.filter(role=User.Role.MEMBER)
    joined = counts_by_bucket(members, "date_joined", period, grain)
    activated = counts_by_bucket(Subscription.objects.all(), "activated_at", period, grain)
    now = admin_summary._members()
    return Section("memberships", _("Memberships"), "members", cards=[
        Card(_("New members"), sum(joined.values())), Card(_("Memberships activated"), sum(activated.values())),
        Card(_("Current memberships"), now["current_memberships"], now=True),
        Card(_("Active members"), now["active"], now=True),
    ], charts=[chart(_("New members and activated memberships"),
                     [(_("New members"), joined), (_("Activated"), activated)], period, grain)],
        breakdowns=[breakdown(_("Activations by source"), _by_choice(
            Subscription.objects.filter(**period.of("activated_at")), "activation_source",
            Subscription._meta.get_field("activation_source").choices))])


def _campaigns(period, grain):
    from apps.advertising.models import Campaign

    created = counts_by_bucket(Campaign.objects.all(), "created_at", period, grain)
    now = admin_summary._counts(Campaign.objects.all(), "status")
    return Section("campaigns", _("Campaigns"), "advertising", cards=[
        Card(_("Campaigns created"), sum(created.values())),
        Card(_("Awaiting review"), now.get(Campaign.Status.SUBMITTED, 0), now=True),
        Card(_("Active"), now.get(Campaign.Status.ACTIVE, 0), now=True),
    ], charts=[chart(_("Campaigns created"), [(_("Created"), created)], period, grain)],
        breakdowns=[breakdown(_("Campaigns created, by status"), _by_choice(
            Campaign.objects.filter(**period.of("created_at")), "status", Campaign.Status.choices))])


def _viewing(period, grain, *, with_rewards):
    from apps.rewards.models import DemoReward
    from apps.viewing.models import WatchSession
    from apps.viewing.services import PERFORMANCE_FIGURES, VERIFIED

    sessions = WatchSession.objects.filter(**period.of("created_at"))
    totals = sessions.aggregate(**PERFORMANCE_FIGURES)
    rate = f"{totals['verified'] / totals['sessions'] * 100:.0f}%" if totals["sessions"] else "—"
    funnel = [[_("Sessions started"), totals["sessions"]], [_("Verified views"), totals["verified"]]]
    if with_rewards:  # The same sessions, by count only: simulated, never money.
        funnel.append([_("Simulated rewards qualified"), DemoReward.objects.filter(
            status=DemoReward.Status.QUALIFIED, session__in=sessions.values("pk")).count()])
    rows = services._campaign_viewing(period, True).rows[:TOP_CAMPAIGNS]
    return Section("viewing", _("Verified viewing"), "advertising", cards=[
        Card(_("Sessions started"), totals["sessions"]), Card(_("Verified views"), totals["verified"]),
        Card(_("Verified rate"), rate), Card(_("Unique viewers"), totals["viewers"]),
        Card(_("Awaiting review"), totals["in_review"]),
    ], charts=[chart(_("Sessions and verified views"), [
        (_("Sessions"), counts_by_bucket(WatchSession.objects.all(), "created_at", period, grain)),
        (_("Verified"), counts_by_bucket(WatchSession.objects.filter(VERIFIED), "created_at", period, grain))],
        period, grain)],
        breakdowns=[breakdown(_("From session to result"), funnel)],
        table={"title": _("Top campaigns by verified views"),
               "columns": [_("Campaign"), _("Advertiser"), _("Sessions"), _("Verified views"), _("Unique viewers")],
               "rows": [row[:5] for row in rows]})


def _support(period, grain):
    from apps.support.models import Ticket
    from apps.support.services import OPEN_STATES

    opened = counts_by_bucket(Ticket.objects.all(), "created_at", period, grain)
    resolved = counts_by_bucket(Ticket.objects.all(), "resolved_at", period, grain)
    open_now = Ticket.objects.filter(status__in=OPEN_STATES).aggregate(
        total=Count("id"), unassigned=Count("id", filter=Q(assignee__isnull=True)))
    return Section("support", _("Customer support"), "support", cards=[
        Card(_("Tickets opened"), sum(opened.values())), Card(_("Tickets resolved"), sum(resolved.values())),
        Card(_("Open tickets"), open_now["total"], now=True), Card(_("Unassigned"), open_now["unassigned"], now=True),
    ], charts=[chart(_("Tickets opened and resolved"), [(_("Opened"), opened), (_("Resolved"), resolved)], period, grain)],
        breakdowns=[breakdown(_("Tickets opened, by category"), _by_choice(
            Ticket.objects.filter(**period.of("created_at")), "category", Ticket.Category.choices))])


def _operations(period, grain):
    from apps.core.models import Job
    from apps.notifications.models import OutboundEmail

    jobs_failed = counts_by_bucket(Job.objects.filter(status=Job.Status.FAILED), "created_at", period, grain)
    emails = counts_by_bucket(OutboundEmail.objects.all(), "created_at", period, grain)
    emails_failed = OutboundEmail.objects.filter(status=OutboundEmail.Status.FAILED, **period.of("created_at")).count()
    return Section("operations", _("Operations"), "operations", cards=[
        Card(_("Failed jobs"), sum(jobs_failed.values())), Card(_("Emails queued"), sum(emails.values())),
        Card(_("Emails failed"), emails_failed), Card(_("Job queue"), admin_summary._jobs()["status_label"], now=True),
    ], charts=[chart(_("Emails queued and failed jobs"), [(_("Emails"), emails), (_("Failed jobs"), jobs_failed)],
                     period, grain)])


def _simulated(period):
    totals = services.reward_totals(period)
    return Section("simulated", _("Simulated rewards"), "rewards", funds="simulated", cards=[
        Card(_("Qualified"), totals["qualified"]), Card(_("Reversed"), totals["reversed"]),
        Card(_("Simulated amount (TZS)"), totals["demo_consumed"])])


def _sandbox(period):
    from apps.payments.models import PaymentIntent

    statuses = {value: row for (value, _name), row in zip(
        PaymentIntent.Status.choices,
        _by_choice(PaymentIntent.objects.filter(**period.of("created_at")), "status", PaymentIntent.Status.choices,
                   amount="amount"))}
    settled = statuses[PaymentIntent.Status.SETTLED]
    return Section("sandbox", _("Sandbox payments"), "payments", funds="sandbox", cards=[
        Card(_("Payments created"), sum(row[1] for row in statuses.values())), Card(_("Settled"), settled[1]),
        Card(_("Settled sandbox amount (TZS)"), settled[2]),
        Card(_("Open exceptions"), admin_summary._payments()["open_exceptions"], now=True)])


def _demo(period):
    from apps.withdrawals.models import DemoWithdrawal

    statuses = {value: row for (value, _name), row in zip(
        DemoWithdrawal.Status.choices,
        _by_choice(DemoWithdrawal.objects.filter(**period.of("created_at")), "status", DemoWithdrawal.Status.choices,
                   amount="simulated_amount"))}
    paid = statuses[DemoWithdrawal.Status.PAID]
    return Section("demo", _("Demo withdrawals"), "withdrawals", funds="demo", cards=[
        Card(_("Requests"), sum(row[1] for row in statuses.values())), Card(_("Paid (simulated)"), paid[1]),
        Card(_("Paid demo amount (TZS)"), paid[2]),
        Card(_("Awaiting review"), admin_summary._withdrawals()["review_queue"], now=True)])


def _budget(period):
    from apps.advertising.models import Campaign
    from apps.advertising.services import campaign_summary

    budgets = campaign_summary(Campaign.objects.filter(**period.of("created_at")))
    return Section("budget", _("Demo budgets"), "advertising", funds="budget", cards=[
        Card(_("In approved campaigns (TZS)"), budgets["demo_budget_running"])])


def _real():
    from apps.advertising.models import Campaign

    wallets = admin_summary._wallets()
    return Section("real", _("Real money"), "payments", funds="real", cards=[
        Card(_("Campaigns with real funding"),
             Campaign.objects.exclude(funding_status=Campaign.Funding.UNFUNDED).count(), now=True),
        Card(_("Unexpected wallet balances"), wallets["unexpected_balance"], now=True)])


# --- The dashboard ---------------------------------------------------------------------------

REPORTS_USED = ("members", "advertising", "rewards", "payments", "withdrawals", "operations", "support")


def can_view(user):
    return any(services.can_view(user, slug) for slug in REPORTS_USED)


def dashboard(user, period):
    """The sections this staff member may see: (operational sections, financial sections)."""
    grain = grain_for(period)
    can = {slug: services.can_view(user, slug) for slug in REPORTS_USED}
    sections, financial = [], []
    if can["members"]:
        sections.append(_memberships(period, grain))
    if can["advertising"]:
        sections.append(_campaigns(period, grain))
        sections.append(_viewing(period, grain, with_rewards=can["rewards"]))
    if can["support"]:
        sections.append(_support(period, grain))
    if can["operations"]:
        sections.append(_operations(period, grain))
    if can["rewards"]:
        financial.append(_simulated(period))
    if can["payments"]:
        financial.append(_sandbox(period))
    if can["withdrawals"]:
        financial.append(_demo(period))
    if can["advertising"]:
        financial.append(_budget(period))
    if can["advertising"] or can["payments"]:
        financial.append(_real())
    return sections, financial
