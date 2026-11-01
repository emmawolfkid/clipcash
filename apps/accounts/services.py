"""
Account lifecycle and access-control services.

Lifecycle:
    register (email only) -> PENDING, unusable password
    activate (emailed link, owner chooses password) -> ACTIVE, email verified
    ACTIVE <-> SUSPENDED, ACTIVE/SUSPENDED/PENDING -> DISABLED,
    DISABLED -> ACTIVE (Super Administrators only). Nothing returns to PENDING.

Auditable actions are recorded through apps.audit.services: business events
inside the same transaction as the change, security events (denials) so that
they survive the rollback of the rejected change. No passwords or tokens are
ever recorded.
"""

import logging

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.core.mail import send_mail
from django.db import IntegrityError, transaction
from django.template.loader import render_to_string
from django.urls import reverse
from django.utils.encoding import force_bytes
from django.utils.http import urlsafe_base64_encode
from django.utils.translation import gettext_lazy as _

from apps.audit.services import (
    DENIED,
    SUCCESS,
    record_business_event,
    record_security_event,
)

from .tokens import verification_token_generator

security_log = logging.getLogger("clipcash.security")

User = get_user_model()
Role = User.Role
Status = User.Status

# Allowed status transitions (creation is not a transition).
STATUS_TRANSITIONS = {
    Status.PENDING: {Status.DISABLED},
    Status.ACTIVE: {Status.SUSPENDED, Status.DISABLED},
    Status.SUSPENDED: {Status.ACTIVE, Status.DISABLED},
    Status.DISABLED: {Status.ACTIVE},
}
CREATION_STATUSES = {Status.PENDING, Status.ACTIVE}


# --- Email -------------------------------------------------------------------


def _send_email(template_prefix, recipient, context):
    subject = render_to_string(f"accounts/email/{template_prefix}_subject.txt", context)
    body = render_to_string(f"accounts/email/{template_prefix}_body.txt", context)
    send_mail(" ".join(subject.split()), body, None, [recipient])


def _token_url(request, url_name, user):
    path = reverse(
        url_name,
        kwargs={
            "uidb64": urlsafe_base64_encode(force_bytes(user.pk)),
            "token": verification_token_generator.make_token(user),
        },
    )
    return request.build_absolute_uri(path)


def _hours(setting_name):
    return max(1, getattr(settings, setting_name) // 3600)


def send_verification_email(user, request):
    """Send the right verification link for the account's state, if any."""
    if user.status == Status.PENDING:
        _send_email(
            "activate_account",
            user.email,
            {
                "url": _token_url(request, "accounts:activate", user),
                "valid_hours": _hours("ACCOUNTS_EMAIL_VERIFICATION_TIMEOUT"),
            },
        )
    elif user.status == Status.ACTIVE and not user.email_verified:
        _send_email(
            "confirm_email",
            user.email,
            {
                "url": _token_url(request, "accounts:confirm_email", user),
                "valid_hours": _hours("ACCOUNTS_EMAIL_VERIFICATION_TIMEOUT"),
            },
        )


# --- Registration and verification ---------------------------------------------


def register(*, email, request):
    """
    Start registration for ``email``. The caller must give the same response in
    every case so the form cannot reveal which addresses are registered.

    * new address: create a PENDING MEMBER with an unusable password and email
      an activation link; the address owner chooses the password;
    * PENDING account: send a fresh activation link (registration retry);
    * ACTIVE account: tell the owner someone tried to register;
    * SUSPENDED/DISABLED: send nothing.
    """
    email = User.objects.normalize_email(email)
    user = User.objects.filter(email=email).first()
    if user is None:
        try:
            with transaction.atomic():
                user = User.objects.create_user(email, password=None, role=Role.MEMBER)
                record_business_event("account.registration.started", actor=None, target=user)
        except (IntegrityError, ValidationError):
            # A concurrent request registered the same address first.
            user = User.objects.filter(email=email).first()
            if user is None:
                raise
        else:
            send_verification_email(user, request)
            return

    record_security_event(
        "account.registration.repeated",
        outcome=SUCCESS,
        actor=None,
        target=user,
        metadata={"account_status": user.status},
    )
    if user.status == Status.PENDING:
        send_verification_email(user, request)
    elif user.status == Status.ACTIVE:
        _send_email(
            "already_registered",
            user.email,
            {
                "login_url": request.build_absolute_uri(reverse("accounts:login")),
                "reset_url": request.build_absolute_uri(reverse("accounts:password_reset")),
            },
        )


def request_verification_email(email, request):
    user = User.objects.filter(
        email=User.objects.normalize_email(email),
        email_verified=False,
        status__in=[Status.PENDING, Status.ACTIVE],
    ).first()
    if user is not None:
        send_verification_email(user, request)


def activate_account(user_pk, token, password):
    """
    Complete registration: verify the email and set the owner's password.

    The token is re-checked while the row is locked, so concurrent or replayed
    submissions cannot both succeed. Returns the user, or None if invalid.
    """
    with transaction.atomic():
        user = User.objects.select_for_update().filter(pk=user_pk).first()
        if (
            user is None
            or user.status != Status.PENDING
            or not token
            or not verification_token_generator.check_token(user, token)
        ):
            return None
        user.set_password(password)
        user.email_verified = True
        user.status = Status.ACTIVE
        user.save()
        record_business_event("account.activated", actor=user, target=user)
    return user


def confirm_email(user_pk, token):
    """Verify the email of an ACTIVE account (e.g. after an address change)."""
    with transaction.atomic():
        user = User.objects.select_for_update().filter(pk=user_pk).first()
        if (
            user is None
            or user.status != Status.ACTIVE
            or user.email_verified
            or not verification_token_generator.check_token(user, token)
        ):
            return None
        user.email_verified = True
        user.save(update_fields=["email_verified", "updated_at"])
        record_business_event("account.email_confirmed", actor=user, target=user)
    return user


# --- Role and status changes --------------------------------------------------


def lock_active_super_admins():
    """
    Lock all ACTIVE Super Administrator rows (in a fixed order, avoiding
    deadlocks) so concurrent changes cannot remove the last one. Must be called
    inside a transaction; the lock is held until it commits.
    """
    list(
        User.objects.select_for_update()
        .filter(role=Role.SUPER_ADMIN, status=Status.ACTIVE)
        .order_by("pk")
        .values_list("pk", flat=True)
    )


def validate_access_change(actor, user, *, role, status):
    """
    Raise ValidationError unless ``actor`` may give ``user`` this role and status.

    ``user`` must hold its stored (pre-change) values; an unsaved ``user`` means
    the account is being created.
    """
    creating = user._state.adding
    if not (actor is not None and actor.is_authenticated and actor.is_active and actor.is_staff):
        _record_access_denial(actor, user, role, status, ["not_staff"])
        raise ValidationError(_("You are not allowed to manage accounts."), code="permission_denied")

    errors = []
    reasons = []

    if not creating and user.role == Role.SUPER_ADMIN:
        lock_active_super_admins()

    if creating:
        if role != Role.MEMBER and not actor.is_superuser:
            errors.append(_("Only Super Administrators can create staff accounts."))
            reasons.append("create_staff_requires_super_admin")
        if status not in CREATION_STATUSES:
            errors.append(_("New accounts must be Pending or Active."))
            reasons.append("invalid_creation_status")
    else:
        if not actor.is_superuser and (user.role != Role.MEMBER or user.pk == actor.pk):
            errors.append(_("Only Super Administrators can manage staff accounts or their own account."))
            reasons.append("staff_or_self_requires_super_admin")
        if role != user.role and not actor.is_superuser:
            errors.append(_("Only Super Administrators can change roles."))
            reasons.append("role_change_requires_super_admin")
        if status != user.status:
            if status not in STATUS_TRANSITIONS[user.status]:
                errors.append(
                    _("An account cannot move from %(old)s to %(new)s.")
                    % {"old": Status(user.status).label, "new": Status(status).label}
                )
                reasons.append("invalid_status_transition")
            elif user.status == Status.DISABLED and not actor.is_superuser:
                errors.append(_("Only Super Administrators can reactivate disabled accounts."))
                reasons.append("reactivation_requires_super_admin")

    if errors:
        _record_access_denial(actor, user, role, status, reasons)
        raise ValidationError(errors, code="permission_denied")


def _record_access_denial(actor, user, role, status, reasons):
    record_security_event(
        "account.access_change.denied",
        outcome=DENIED,
        actor=actor,
        target=None if user._state.adding else user,
        metadata={"requested_role": role, "requested_status": status, "reasons": reasons},
    )


def log_access_change(actor, user, *, old_role, old_status):
    """Record role/status changes. Call inside the transaction that saved them."""
    if old_role != user.role:
        record_business_event(
            "account.role_changed",
            actor=actor,
            target=user,
            metadata={"from_role": old_role, "to_role": user.role},
        )
    if old_status != user.status:
        record_business_event(
            "account.status_changed",
            actor=actor,
            target=user,
            metadata={"from_status": old_status, "to_status": user.status},
        )


@transaction.atomic
def change_access(actor, user, *, role=None, status=None):
    """Centralized role/status assignment with authorization and invariants."""
    if User.objects.filter(pk=user.pk, role=Role.SUPER_ADMIN).exists():
        lock_active_super_admins()  # Same lock order as validate_access_change.
    user = User.objects.select_for_update().get(pk=user.pk)
    new_role = role or user.role
    new_status = status or user.status
    validate_access_change(actor, user, role=new_role, status=new_status)

    old_role, old_status = user.role, user.status
    user.role, user.status = new_role, new_status
    user.full_clean()  # Includes the last-active-Super-Administrator guard.
    user.save()
    log_access_change(actor, user, old_role=old_role, old_status=old_status)
    return user


def assign_role(actor, user, role):
    return change_access(actor, user, role=role)


def change_status(actor, user, status):
    return change_access(actor, user, status=status)
