"""
Account lifecycle and access-control services.

Lifecycle:
    PRD-01: register_member (name + password + ONE of email or Tanzanian mobile number)
        email -> PENDING; the emailed link (confirm_email) -> ACTIVE, email verified
        phone -> ACTIVE at once, number unverified (no SMS in this phase)
    REG-02: register_with_phone (mobile number + password) -> ACTIVE member, no email
    Accounts created PENDING before PRD-01 (or by staff) still activate through the
    emailed link: activate (owner chooses password) -> ACTIVE, email verified
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
from django.db import IntegrityError, transaction
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from apps.audit.services import (
    DENIED,
    SUCCESS,
    hash_identifier,
    record_business_event,
    record_security_event,
)

from apps.notifications.services import base_url, enqueue_email

from . import emails
from .access import demo_unconfirmed
from .models import PendingEmailChange
from .tokens import email_change_token_generator, verification_token_generator

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


# --- Email (durable outbox; see apps.accounts.emails) ------------------------------


def send_verification_email(user, request):
    """Queue the right verification email for the account's state, if any.
    Delivery happens after commit; nothing is sent if the caller rolls back."""
    if not user.email:  # REG-02/REG-03: phone- and username-only accounts have no address to verify.
        return
    if is_awaiting_email_confirmation(user):
        # PRD-01: an email sign-up already chose its password; the link only confirms and activates.
        enqueue_email(emails.CONFIRM_EMAIL, recipient=user.email, user=user,
                      context={"base_url": base_url(request)})
    elif user.status == Status.PENDING:
        enqueue_email(emails.ACTIVATION, recipient=user.email, user=user,
                      context={"base_url": base_url(request)})
    elif user.status == Status.ACTIVE and not user.email_verified:
        enqueue_email(emails.CONFIRM_EMAIL, recipient=user.email, user=user,
                      context={"base_url": base_url(request)})


# --- Registration and verification ---------------------------------------------


class DuplicateIdentifier(Exception):
    """REG-03: the chosen sign-in identifier already belongs to an account."""

    def __init__(self, method):
        super().__init__(method)
        self.method = method


class DuplicatePhoneNumber(DuplicateIdentifier):
    """The mobile number already belongs to an account (in any format)."""

    def __init__(self, method="phone"):
        super().__init__(method)


# method -> the User field holding that (already normalized) identifier.
REGISTRATION_FIELDS = {"phone": "phone_number", "email": "email", "username": "username"}
CONTACT_METHODS = ("email", "phone")


def is_awaiting_email_confirmation(user):
    """PRD-01: an email sign-up whose address is not confirmed yet. It cannot sign in (only
    ACTIVE accounts can) and becomes ACTIVE only through the emailed link."""
    return bool(user.status == Status.PENDING and user.email_ownership_required and user.email
                and not user.email_verified)


def register_member(*, contact_method, contact, password, first_name, last_name, request=None):
    """
    PRD-01: create ONE member from the public sign-up form: a first and last name, a password
    and the sign-in identifier the member chose, ``contact_method`` "email" (a normalized
    address) or "phone" (a normalized +2556/7XXXXXXXX number). No username.

    * Email: the account is created PENDING with its password, and the confirmation link is
      queued in the same transaction. Opening the link confirms the address and activates
      the account (confirm_email); until then it cannot sign in or reset a password. Returns
      the new account, or None when the address already belongs to an account: nothing is
      created, the address's owner is emailed instead (a fresh link if that account is still
      awaiting confirmation, otherwise an "already registered" notice), and the caller shows
      the same page either way, so the form does not reveal which addresses are registered.
    * Phone: the account is ACTIVE at once and signs in with the number, which stays
      unverified (no SMS in this phase). It cannot start a paid membership or recover a
      password until the member adds and confirms an email address. A number already in
      use raises DuplicatePhoneNumber (instant sign-up cannot hide it; attempts are
      rate-limited by the view).

    The unique constraints decide duplicates, including concurrent requests, in any letter
    case or number format.
    """
    if contact_method not in CONTACT_METHODS:
        raise ValueError(f"Unknown contact method {contact_method!r}.")
    by_email = contact_method == "email"
    field = REGISTRATION_FIELDS[contact_method]
    try:
        with transaction.atomic():
            user = User.objects.create_user(
                contact if by_email else None, password, first_name=first_name, last_name=last_name,
                role=Role.MEMBER, status=Status.PENDING if by_email else Status.ACTIVE,
                email_ownership_required=True, **({} if by_email else {"phone_number": contact}))
            record_business_event("account.registered", actor=user, target=user, metadata={"method": contact_method})
            if by_email:
                send_verification_email(user, request)
    except (IntegrityError, ValidationError):
        existing = User.objects.filter(**{field: contact}).first()
        if existing is None:
            raise
        record_security_event("account.registration.duplicate", outcome=DENIED, actor=None, target=existing,
                              metadata={"method": contact_method})
        if not by_email:
            raise DuplicatePhoneNumber from None
        _email_existing_owner(existing, request)
        return None
    return user


def _email_existing_owner(existing, request):
    """Someone signed up with an address that already has an account: tell its inbox, never
    the visitor. Suspended or disabled accounts get nothing (the builders skip them)."""
    if is_awaiting_email_confirmation(existing):
        send_verification_email(existing, request)
    elif existing.status == Status.ACTIVE:
        enqueue_email(emails.ALREADY_REGISTERED, recipient=existing.email, user=existing,
                      context={"base_url": base_url(request)})


def register_with_phone(*, phone_number, password):
    """REG-02 helper (no username), kept for existing callers and tests; the public form uses
    register_member(). Creates an ACTIVE member that signs in with its mobile number."""
    try:
        with transaction.atomic():
            user = User.objects.create_user(None, password, phone_number=phone_number, role=Role.MEMBER,
                                            status=Status.ACTIVE)
            record_business_event("account.registered", actor=user, target=user, metadata={"method": "phone"})
    except (IntegrityError, ValidationError):
        existing = User.objects.filter(phone_number=phone_number).first()
        if existing is None:
            raise
        record_security_event("account.registration.duplicate", outcome=DENIED, actor=None, target=existing,
                              metadata={"method": "phone"})
        raise DuplicatePhoneNumber from None
    return user


def email_password_changed(user, request):
    """PRD-01: tell the account's CONFIRMED address that its password changed (change or reset).
    Queued in the caller's transaction; nothing goes to an unconfirmed address."""
    if user.has_verified_email:
        enqueue_email(emails.PASSWORD_CHANGED, recipient=user.email, user=user,
                      context={"base_url": base_url(request)})


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
            or user.email_ownership_required  # PRD-01 sign-ups activate through confirm_email.
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
    """Verify the email of an ACTIVE account (e.g. after an address change), or, PRD-01,
    confirm and activate an email sign-up (PENDING -> ACTIVE, email verified). The token is
    re-checked on the locked row; confirming changes the state it signs, so a replayed or
    concurrent submission fails. Returns the user, or None for any invalid link."""
    with transaction.atomic():
        user = User.objects.select_for_update().filter(pk=user_pk).first()
        if (
            user is None
            or not (user.status == Status.ACTIVE or is_awaiting_email_confirmation(user))
            or user.email_verified
            or not verification_token_generator.check_token(user, token)
        ):
            return None
        activating = user.status == Status.PENDING
        user.email_verified = True
        if activating:
            user.status = Status.ACTIVE
            user.save()  # Also derives is_active.
            record_business_event("account.activated", actor=user, target=user, metadata={"method": "email"})
        else:
            user.save(update_fields=["email_verified", "updated_at"])
        record_business_event("account.email_confirmed", actor=user, target=user)
        notify_account(user, "email_confirmed", "Your email address is confirmed",
                       "You can now use every member feature that needs a confirmed address.",
                       key=f"email_confirmed:{user.pk}:{user.email}")
    return user


PROFILE_FIELDS = ("first_name", "last_name", "phone_number")


def update_profile(user, **values):
    """Save the owner's own contact details. Only PROFILE_FIELDS and only the changed
    ones are written, on a freshly locked row, so a role or status change made while the
    form was open is never overwritten. Returns the names of the changed fields."""
    with transaction.atomic():
        account = User.objects.select_for_update().get(pk=user.pk)
        changed = [name for name in PROFILE_FIELDS if getattr(account, name) != values[name]]
        if not changed:
            return []
        for name in changed:
            setattr(account, name, values[name])
        account.save(update_fields=[*changed, "updated_at"])
        record_business_event("account.profile_updated", actor=account, target=account,
                              metadata={"changed_fields": changed})
        if "phone_number" in changed:
            notify_account(account, "phone_changed", "Your phone number was changed",
                           "If this was not you, change your password now and contact ClipCash.",
                           key=f"phone_changed:{account.pk}:{timezone.now().isoformat()}")
    return changed


# --- Email address change ---------------------------------------------------------


def email_change_available(user):
    """
    Closed to demo accounts using the verification waiver: they sign in because their
    address is on the allowlist and must not swap it. Also closed when the site runs
    the ``dummy`` email backend, because the confirmation link could never arrive.
    """
    return not demo_unconfirmed(user) and "dummy" not in settings.EMAIL_BACKEND


def pending_email_change(user):
    change = PendingEmailChange.objects.filter(user=user).first()
    return None if change is None or change.is_expired else change


def request_email_change(user, new_email, request):
    """
    Start a change to ``new_email``. The account keeps its address until the new inbox
    confirms. If the address already belongs to an account, everything looks the same to
    the requester (the request is shown as waiting) but no link is sent, so this cannot
    be used to find out which addresses are registered.
    """
    new_email = User.objects.normalize_email(new_email)
    with transaction.atomic():
        account = User.objects.select_for_update().get(pk=user.pk)
        PendingEmailChange.objects.filter(user=account).delete()  # Ends any earlier link.
        change = PendingEmailChange.objects.create(user=account, new_email=new_email)
        available = not User.objects.filter(email=new_email).exists()
        record_business_event("account.email_change.requested", actor=account, target=account,
                              metadata={"new_address_hash": hash_identifier(new_email),
                                        "address_available": available})
        site = base_url(request)
        if available:
            enqueue_email(emails.EMAIL_CHANGE_CONFIRM, recipient=new_email, user=account,
                          context={"base_url": site, "change": str(change.pk)})
        if account.email:  # REG-02: a phone-only member adding a first address has no old one to warn.
            enqueue_email(emails.EMAIL_CHANGE_REQUESTED, recipient=account.email, user=account,
                          context={"base_url": site})
        notify_account(account, "email_change_requested", "An email address change was requested",
                       "Nothing changes until the new address is confirmed. If this was not you, "
                       "cancel it from your account and change your password.",
                       key=f"email_change_requested:{change.pk}")
    return change


def cancel_email_change(user):
    """Withdraw the owner's waiting change, if any. Its link stops working."""
    with transaction.atomic():
        deleted, _details = PendingEmailChange.objects.filter(user=user).delete()
        if deleted:
            record_business_event("account.email_change.cancelled", actor=user, target=user)
    return bool(deleted)


def discard_email_change(user):
    """A password change or reset ends a waiting change without its own audit event:
    the request may have come from whoever the owner is locking out."""
    PendingEmailChange.objects.filter(user=user).delete()


def confirm_email_change(change_pk, token, user):
    """
    Complete a change from the link sent to the new address, for ``user``, the signed-in
    account that has just re-verified its password (RG-03; see views.EmailChangeReviewView).
    A link belonging to another account is refused and left usable by its owner. Checked
    again on the locked rows, so a replay or concurrent submission cannot succeed twice.
    Returns the account, or None for any invalid link (including an address taken since
    it was requested).

    Locks the account before the request, the order every other path uses (new request,
    password change, password reset); the opposite order deadlocks against them.
    """
    owner_id = PendingEmailChange.objects.filter(pk=change_pk).values_list("user_id", flat=True).first()
    if owner_id is None or user is None or owner_id != user.pk:
        return None
    try:
        with transaction.atomic():
            account = User.objects.select_for_update().filter(pk=owner_id).first()
            change = PendingEmailChange.objects.select_for_update().filter(pk=change_pk, user_id=owner_id).first()
            if account is None or change is None or account.status != Status.ACTIVE:
                return None
            change.user = account  # The token is checked against the locked account.
            if not email_change_token_generator.check_token(change, token):
                return None
            new_email = change.new_email
            if User.objects.filter(email=new_email).exclude(pk=account.pk).exists():
                change.delete()
                return None
            old_email = account.email
            account.email = new_email
            account.save(update_fields=["email", "updated_at"])  # Clears email_verified.
            account.email_verified = True  # The owner of this address has just proved it.
            account.save(update_fields=["email_verified", "updated_at"])
            change.delete()
            record_business_event("account.email_changed", actor=account, target=account,
                                  metadata={"previous_address_hash": hash_identifier(old_email),
                                            "new_address_hash": hash_identifier(new_email)})
            if old_email:
                enqueue_email(emails.EMAIL_CHANGED, recipient=old_email, user=account, context={})
            notify_account(account, "email_changed", "Your email address was changed",
                           "Use the new address to sign in. If this was not you, contact ClipCash now.",
                           key=f"email_changed:{change_pk}")
    except IntegrityError:  # A concurrent registration took the address first.
        return None
    return account


def notify_account(user, event, title, message, *, key):
    """In-app account and security notice (never emailed here: the account emails keep
    their existing behaviour). No codes, tokens or addresses are included."""
    from apps.notifications import inbox

    inbox.notify(user, f"account.{event}", category=inbox.Category.ACCOUNT, title=title, message=message,
                 link=inbox.link_to("accounts:profile"), dedupe_key=f"account.{key}")


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
        if user.status == Status.ACTIVE and old_status in (Status.SUSPENDED, Status.DISABLED):
            # Suspended or disabled accounts cannot sign in to read a notice; reactivation is shown.
            notify_account(user, "reactivated", "Your account is active again",
                           "You can sign in and use ClipCash as before.",
                           key=f"reactivated:{user.pk}:{timezone.now().isoformat()}")


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
