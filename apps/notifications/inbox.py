"""
In-app notifications (Phase 15-A).

Business services call ``notify`` inside their own transaction, so a notice exists
only if the change it describes commits, and disappears with it on rollback. Each
notice has a ``dedupe_key`` unique per recipient: retries, repeated jobs and
replayed requests add nothing (INSERT ... ON CONFLICT DO NOTHING, which never
breaks the caller's transaction).

Selected notices are also emailed through the existing outbox (``email=True``):
the email is queued in the same transaction and delivered later by the worker, so a
delivery failure can never undo the business change. The email carries only the
notice's title and message and asks the reader to sign in; it contains no link,
token or account data.

This module never imports business apps; they import it.
"""

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.db.models import Q
from django.urls import reverse
from django.utils import timezone
from django.utils.http import url_has_allowed_host_and_scheme

from .models import Notification
from .services import SkipEmail, enqueue_email, register_email_kind

Category = Notification.Category
NOTICE_EMAIL = "notifications.notice"
MAX_TITLE = 120
MAX_MESSAGE = 400


def when(moment):
    """A date and time for notice text, in the site time zone (e.g. "4 Nov 2026, 18:00 EAT")."""
    import zoneinfo

    from apps.core.services import get_site_configuration

    zone = zoneinfo.ZoneInfo(get_site_configuration().default_timezone)
    local = moment.astimezone(zone)
    return f"{local.day} {local:%b %Y, %H:%M} {local.tzname()}"


def link_to(url_name, *args):
    """An internal path for a notice (resolved now, so a typo fails loudly)."""
    return reverse(url_name, args=args)


def safe_link(link):
    """The stored link if it is an internal path, else "" (defence in depth; the
    database also refuses anything but "/..." paths)."""
    link = (link or "").strip()
    if not link or not link.startswith("/") or link.startswith("//") or "\\" in link:
        return ""
    return link if url_has_allowed_host_and_scheme(link, allowed_hosts=None) else ""


def notify(recipients, kind, *, category, title, message="", link="", dedupe_key, email=False):
    """Create one notice per recipient (an account or an iterable of accounts).

    Inactive accounts and duplicates are skipped silently. Returns the number of new
    notices. ``dedupe_key`` must identify the event (e.g. "campaign.approved:<id>:<rev>").
    """
    if not dedupe_key or len(dedupe_key) > 160:
        raise ValidationError("A notification needs an event key of at most 160 characters.")
    if not title:
        raise ValidationError("A notification needs a title.")
    link = safe_link(link)
    if not hasattr(recipients, "__iter__"):
        recipients = [recipients]
    accounts = [user for user in recipients if user is not None and getattr(user, "is_active", False)]
    if not accounts:
        return 0
    existing = set(Notification.objects.filter(recipient__in=accounts, dedupe_key=dedupe_key)
                   .values_list("recipient_id", flat=True))
    created = 0
    for user in {user.pk: user for user in accounts}.values():
        if user.pk in existing:
            continue
        try:
            with transaction.atomic():  # Savepoint: a duplicate never breaks the caller's transaction.
                notice = Notification.objects.create(
                    recipient=user, category=category, kind=kind[:64], title=title[:MAX_TITLE],
                    message=message[:MAX_MESSAGE], link=link, dedupe_key=dedupe_key)
        except IntegrityError:
            continue  # A concurrent writer stored this event for this account first.
        created += 1
        # Only the writer that created the notice queues its email, and only to a verified
        # address (REG-02: phone-only members, and unconfirmed addresses, get the inbox notice only).
        if email and user.email and user.email_verified:
            enqueue_email(NOTICE_EMAIL, recipient=user.email, user=user, context={"notification": str(notice.pk)})
    return created


def staff_with_permission(*perms):
    """Active staff who hold any of ``perms`` ("app.codename"), superusers included."""
    User = get_user_model()
    condition = Q(is_superuser=True)
    for dotted in perms:
        app_label, codename = dotted.split(".")
        condition |= Q(user_permissions__content_type__app_label=app_label, user_permissions__codename=codename)
        condition |= Q(groups__permissions__content_type__app_label=app_label, groups__permissions__codename=codename)
    return list(User.objects.filter(condition, is_active=True, is_staff=True).distinct())


# --- Reading and marking ---------------------------------------------------------------------


def for_recipient(user, *, category="", unread=False):
    notices = Notification.objects.filter(recipient=user)
    if category in Category.values:
        notices = notices.filter(category=category)
    if unread:
        notices = notices.filter(read_at__isnull=True)
    return notices.order_by("-created_at")


def unread_count(user):
    if not getattr(user, "is_authenticated", False):
        return 0
    return Notification.objects.filter(recipient=user, read_at__isnull=True).count()


def get_for_recipient(user, notification_id):
    """The user's own notice, or None (another account's notice looks nonexistent)."""
    return Notification.objects.filter(recipient=user, pk=notification_id).first()


def mark_read(user, notification_id):
    """Mark one of the user's own notices read. Returns the notice or None. Idempotent."""
    Notification.objects.filter(recipient=user, pk=notification_id, read_at__isnull=True).update(
        read_at=timezone.now(), updated_at=timezone.now())
    return get_for_recipient(user, notification_id)


def mark_all_read(user):
    now = timezone.now()
    return Notification.objects.filter(recipient=user, read_at__isnull=True).update(read_at=now, updated_at=now)


# --- Email copy of a notice --------------------------------------------------------------------


def _build_notice_email(email):
    notice = Notification.objects.filter(pk=email.context.get("notification")).select_related("recipient").first()
    if notice is None:
        raise SkipEmail("notification_removed")
    if not notice.recipient.is_active:
        raise SkipEmail("account_inactive")
    recipient = notice.recipient
    if recipient.email != email.recipient or not recipient.email_verified:
        raise SkipEmail("address_changed")  # REG-02: only the verified address it was queued for.
    body = (f"{notice.title}\n\n{notice.message}\n\n"
            "Sign in to ClipCash to see the details in your notifications.\n")
    return f"ClipCash: {notice.title}", body


register_email_kind(NOTICE_EMAIL, builder=_build_notice_email, context_keys=("notification",),
                    ttl_seconds=lambda: 60 * 60 * 24 * 3)
