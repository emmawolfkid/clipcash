"""
Account email kinds for the durable outbox (apps.notifications).

Each builder runs when the message is sent. It reloads the account, skips the
message if it no longer applies (status or address changed), and generates the
link token dated to the original request (``token_issued_at``), so a delayed
or retried delivery never extends the link's validity. Tokens exist only in
the delivered email.
"""

from urllib.parse import urlsplit

from django.conf import settings
from django.contrib.auth import get_user_model
from django.template.loader import render_to_string
from django.urls import reverse
from django.utils import timezone
from django.utils.encoding import force_bytes
from django.utils.http import urlsafe_base64_encode

from apps.notifications.services import SkipEmail, register_email_kind

from .tokens import email_change_token_generator, password_reset_token_generator, verification_token_generator

ACTIVATION = "accounts.activation"
CONFIRM_EMAIL = "accounts.confirm_email"
PASSWORD_RESET = "accounts.password_reset"
ALREADY_REGISTERED = "accounts.already_registered"
EMAIL_CHANGE_CONFIRM = "accounts.email_change_confirm"
EMAIL_CHANGE_REQUESTED = "accounts.email_change_requested"
EMAIL_CHANGED = "accounts.email_changed"


def _render(template_prefix, context):
    subject = render_to_string(f"accounts/email/{template_prefix}_subject.txt", context)
    body = render_to_string(f"accounts/email/{template_prefix}_body.txt", context)
    return subject, body


def _account(email, *, statuses, verified=None):
    User = get_user_model()
    user = User.objects.filter(pk=email.user_id).first()
    if user is None:
        raise SkipEmail("account_deleted")
    if user.email != email.recipient:
        raise SkipEmail("address_changed")
    if user.status not in statuses:
        raise SkipEmail("status_changed")
    if verified is not None and user.email_verified != verified:
        raise SkipEmail("verification_changed")
    return user


def _token_at_issue(generator, user, issued_at):
    """A token dated to when the email was requested, not when it is sent."""
    age = int((timezone.now() - issued_at).total_seconds())
    timestamp = generator._num_seconds(generator._now()) - max(0, age)
    return generator._make_token_with_timestamp(user, timestamp, generator.secret)


def _uid(user):
    return urlsafe_base64_encode(force_bytes(user.pk))


def _hours(setting_name):
    return max(1, getattr(settings, setting_name) // 3600)


def build_activation(email):
    User = get_user_model()
    user = _account(email, statuses=[User.Status.PENDING])
    token = _token_at_issue(verification_token_generator, user, email.token_issued_at)
    path = reverse("accounts:activate", kwargs={"uidb64": _uid(user), "token": token})
    return _render("activate_account", {"url": email.context["base_url"] + path,
                                        "valid_hours": _hours("ACCOUNTS_EMAIL_VERIFICATION_TIMEOUT")})


def build_confirm_email(email):
    User = get_user_model()
    user = _account(email, statuses=[User.Status.ACTIVE], verified=False)
    token = _token_at_issue(verification_token_generator, user, email.token_issued_at)
    path = reverse("accounts:confirm_email", kwargs={"uidb64": _uid(user), "token": token})
    return _render("confirm_email", {"url": email.context["base_url"] + path,
                                     "valid_hours": _hours("ACCOUNTS_EMAIL_VERIFICATION_TIMEOUT")})


def build_password_reset(email):
    User = get_user_model()
    user = _account(email, statuses=[User.Status.ACTIVE])
    if not user.has_usable_password():
        raise SkipEmail("no_usable_password")
    token = _token_at_issue(password_reset_token_generator, user, email.token_issued_at)
    parts = urlsplit(email.context["base_url"])
    return _render("password_reset", {"protocol": parts.scheme, "domain": parts.netloc,
                                      "uid": _uid(user), "token": token})


def build_already_registered(email):
    User = get_user_model()
    _account(email, statuses=[User.Status.ACTIVE])
    base = email.context["base_url"]
    return _render("already_registered", {"login_url": base + reverse("accounts:login"),
                                          "reset_url": base + reverse("accounts:password_reset")})


def build_email_change_confirm(email):
    """Sent to the new address. The token is signed here and exists only in the email."""
    from .models import PendingEmailChange

    change = (PendingEmailChange.objects.select_related("user")
              .filter(pk=email.context["change"], user_id=email.user_id, new_email=email.recipient).first())
    if change is None:
        raise SkipEmail("change_withdrawn")
    if change.user.status != get_user_model().Status.ACTIVE:
        raise SkipEmail("status_changed")
    token = _token_at_issue(email_change_token_generator, change, email.token_issued_at)
    path = reverse("accounts:email_change_confirm", kwargs={"pk": change.pk, "token": token})
    return _render("email_change_confirm", {"url": email.context["base_url"] + path,
                                            "valid_hours": _hours("ACCOUNTS_EMAIL_VERIFICATION_TIMEOUT")})


def build_email_change_requested(email):
    """Warning to the current address. Holds no link to act on and not the new address."""
    _account(email, statuses=[get_user_model().Status.ACTIVE])
    return _render("email_change_requested", {"account_url": email.context["base_url"] + reverse("accounts:profile")})


def build_email_changed(email):
    """Sent to the previous address, so the address check in _account does not apply."""
    if not get_user_model().objects.filter(pk=email.user_id).exists():
        raise SkipEmail("account_deleted")
    return _render("email_changed", {})


def register_kinds():
    register_email_kind(EMAIL_CHANGE_CONFIRM, builder=build_email_change_confirm,
                        ttl_seconds=lambda: settings.ACCOUNTS_EMAIL_VERIFICATION_TIMEOUT,
                        context_keys=("base_url", "change"))
    register_email_kind(EMAIL_CHANGE_REQUESTED, builder=build_email_change_requested,
                        ttl_seconds=lambda: settings.NOTIFICATIONS_DEFAULT_EMAIL_TTL)
    register_email_kind(EMAIL_CHANGED, builder=build_email_changed,
                        ttl_seconds=lambda: settings.NOTIFICATIONS_DEFAULT_EMAIL_TTL, context_keys=())
    register_email_kind(ACTIVATION, builder=build_activation,
                        ttl_seconds=lambda: settings.ACCOUNTS_EMAIL_VERIFICATION_TIMEOUT)
    register_email_kind(CONFIRM_EMAIL, builder=build_confirm_email,
                        ttl_seconds=lambda: settings.ACCOUNTS_EMAIL_VERIFICATION_TIMEOUT)
    register_email_kind(PASSWORD_RESET, builder=build_password_reset,
                        ttl_seconds=lambda: settings.ACCOUNTS_PASSWORD_RESET_TIMEOUT)
    register_email_kind(ALREADY_REGISTERED, builder=build_already_registered,
                        ttl_seconds=lambda: settings.NOTIFICATIONS_DEFAULT_EMAIL_TTL)
