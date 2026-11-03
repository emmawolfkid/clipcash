"""
Check whether the end-to-end client demonstration can run here (read-only).

    python manage.py demo_readiness

Reports every precondition of the member and advertiser demo journeys (memberships,
playable videos, eligible campaigns, reward policies, worker, email, staff for
two-person approvals, financial locks) as PASS, WARN or FAIL, using the same
decision functions the application uses. Exits with status 1 if anything FAILs.
It changes nothing and contacts no external service.
"""

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand
from django.db import connection
from django.db.models import Q
from django.utils import timezone

from apps.administration import summary
from apps.advertising import services as advertising
from apps.advertising.models import Campaign
from apps.core import features
from apps.memberships import services as memberships
from apps.memberships.models import MembershipPlan, Subscription
from apps.payments.providers import sandbox_active
from apps.rewards.models import RewardPolicy
from apps.videos.models import Video
from apps.videos.providers import demo_allowed

PASS, WARN, FAIL = "PASS", "WARN", "FAIL"
LOCK_TRIGGERS = (
    "ledger_account_guard", "ledger_line_apply", "ledger_entry_immutable", "ledger_line_immutable",
    "ledger_line_balanced", "ledger_entry_complete", "a_wallets_block_member_postings",
    "wallets_transactions_locked", "withdrawals_request_locked", "advertising_campaign_guard",
    "payments_intent_guard", "membership_subscription_guard", "audit_auditevent_append_only",
)
LOCK_CONSTRAINTS = ("adv_campaign_funding_locked", "sub_activation_source_consistent")


def readiness():
    """[(status, area, detail)] for this environment."""
    results = []

    def add(ok, area, good, bad, *, severity=FAIL):
        results.append((PASS if ok else severity, area, good if ok else bad))

    # Configuration and features.
    add(settings.RESTRICTED_DEMO_MODE, "Restricted demo mode", "On: public registration closed.",
        "Off: public registration follows the site configuration.", severity=WARN)
    for key in ("memberships", "videos", "advertisers"):
        add(features.is_enabled(key), f"Feature: {key}", "Enabled.",
            "Disabled in Site configuration (Django admin).")

    # Video source.
    if settings.VIDEO_PROVIDER == "bunny":
        missing = [name for name in ("BUNNY_STREAM_LIBRARY_ID", "BUNNY_STREAM_API_KEY", "BUNNY_STREAM_CDN_HOSTNAME",
                                     "BUNNY_STREAM_TOKEN_SECURITY_KEY") if not getattr(settings, name)]
        add(not missing, "Video provider", "Bunny Stream configured (not contacted by this check: verify "
            "playback and CDN token authentication manually).", "Bunny Stream settings missing: " + ", ".join(missing))
    else:
        add(demo_allowed(), "Video provider", "Demo provider allowed here.",
            "Demo provider not allowed (needs DEBUG or VIDEO_DEMO_ALLOWED on an isolated demo deployment).")
        add(bool(settings.VIDEO_DEMO_SAMPLE_URL), "Demo sample clip",
            "VIDEO_DEMO_SAMPLE_URL is set (use a clip you made or are licensed to use; set each demo video's "
            "duration to the clip's real length).",
            "VIDEO_DEMO_SAMPLE_URL is empty: the player shows a placeholder and no view can be verified.")

    min_seconds = settings.VIEWING_MIN_VIDEO_SECONDS
    playable = Video.objects.filter(publication_status=Video.Publication.PUBLISHED,
                                    processing_status=Video.Processing.READY)
    verifiable = playable.filter(duration_seconds__gte=min_seconds)
    add(verifiable.exists(), "Playable videos",
        f"{verifiable.count()} published, ready, with a known length of at least {min_seconds} s.",
        f"None published and ready with a known length of at least {min_seconds} s "
        f"({playable.count()} published and ready).")

    # Memberships.
    plans = MembershipPlan.objects.filter(status=MembershipPlan.Status.ACTIVE)
    add(plans.exists(), "Membership plans", f"{plans.count()} active.", "No active plan (Django admin).")
    now = timezone.now()
    members = (Subscription.objects.filter(status=Subscription.Status.ACTIVE, starts_at__lte=now, expires_at__gt=now)
               .values("member").distinct().count())
    add(members > 0, "Members with access", f"{members} with a current membership.",
        "No member has a current membership: grant a complimentary demo membership from /manage/members/.",
        severity=WARN)
    add(memberships.complimentary_grants_available(), "Complimentary demo grants", "Available to Super Administrators.",
        "Unavailable (needs DJANGO_MEMBERSHIPS_COMPLIMENTARY_GRANTS=True and restricted demo mode).",
        severity=FAIL if members == 0 else WARN)

    # Campaigns and demo rewards.
    eligible = [c for c in Campaign.objects.select_related("organization", "video", "video__category")
                .filter(status=Campaign.Status.ACTIVE) if advertising.campaign_eligibility(c)]
    add(bool(eligible), "Sponsored campaigns", f"{len(eligible)} active and eligible to be shown.",
        "No campaign is active and eligible (approved advertiser, granted ready video, current dates).")
    covered = set()
    for plan in plans:
        covered.update(plan.entitlements)
    uncovered = [c.name for c in eligible if c.video.required_entitlement and c.video.required_entitlement not in covered]
    add(not uncovered, "Plan entitlements", "Every eligible campaign's video is covered by an active plan.",
        "No active plan grants the entitlement for: " + ", ".join(uncovered))
    with_policy = RewardPolicy.objects.filter(status=RewardPolicy.Status.APPROVED,
                                              campaign__in=[c.pk for c in eligible]).values("campaign").distinct().count()
    add(with_policy > 0, "Demo reward policies", f"{with_policy} eligible campaign(s) have an approved policy.",
        "No eligible campaign has an approved demo reward policy: verified views will record no simulated reward.",
        severity=WARN)

    # Operations.
    jobs = summary._jobs()
    add(jobs["status"] == summary.HEALTHY, "Background worker", "Heartbeat fresh.",
        f"Worker status: {jobs['status_label']}. Emails stay queued and demo rewards need the console's "
        "'evaluate' action until `run_jobs` runs.", severity=WARN)
    delivering = not any(name in settings.EMAIL_BACKEND for name in ("console", "locmem", "dummy", "filebased"))
    add(delivering, "Email", "A delivering backend is configured (verify with email_diagnostics).",
        "The email backend does not deliver: demo accounts must be prepared without email.", severity=WARN)
    approvers = (get_user_model().objects.filter(is_active=True, is_staff=True)
                 .filter(Q(is_superuser=True) | Q(user_permissions__codename="approve_reward_policies")
                         | Q(groups__permissions__codename="approve_reward_policies")).distinct().count())
    add(approvers >= 2, "Two-person approvals", f"{approvers} staff can approve reward policies.",
        f"Only {approvers} staff can approve reward policies; four-eyes steps need a second person "
        "(decision D2).", severity=WARN)

    # Financial safety (must always hold).
    with connection.cursor() as cursor:
        cursor.execute("SELECT count(*) FROM pg_trigger WHERE tgname = ANY(%s) AND tgenabled = 'O'",
                       [list(LOCK_TRIGGERS)])
        (triggers,) = cursor.fetchone()
        cursor.execute("SELECT count(*) FROM pg_constraint WHERE conname = ANY(%s)", [list(LOCK_CONSTRAINTS)])
        (constraints,) = cursor.fetchone()
    add(triggers == len(LOCK_TRIGGERS) and constraints == len(LOCK_CONSTRAINTS), "Financial locks",
        f"All {len(LOCK_TRIGGERS)} lock triggers enabled and {len(LOCK_CONSTRAINTS)} lock constraints present.",
        f"Lock triggers {triggers}/{len(LOCK_TRIGGERS)}, constraints {constraints}/{len(LOCK_CONSTRAINTS)}.")
    locked = not any(features.is_enabled(key) for key in features.FINANCIAL_FEATURES)
    add(locked, "Real money", "Real payments, rewards and withdrawals are locked.", "A financial feature is enabled.")
    results.append((PASS, "Sandbox payments",
                    "Active: sandbox payment and payout stories can be shown." if sandbox_active() else
                    "Disabled: payment and payout simulations are not part of this demo."))
    return results


class Command(BaseCommand):
    help = "Check the preconditions of the client demonstration (read-only)."

    def handle(self, *args, **options):
        results = readiness()
        styles = {PASS: self.style.SUCCESS, WARN: self.style.WARNING, FAIL: self.style.ERROR}
        for status, area, detail in results:
            self.stdout.write(styles[status](f"{status:4}  {area}: {detail}"))
        failed = sum(1 for status, _, _ in results if status == FAIL)
        warned = sum(1 for status, _, _ in results if status == WARN)
        if failed:
            self.stdout.write(self.style.ERROR(f"\nNOT READY: {failed} failing, {warned} warnings."))
            raise SystemExit(1)
        self.stdout.write(self.style.SUCCESS(f"\nREADY ({warned} warnings)."))
