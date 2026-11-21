import uuid
from datetime import timedelta

from django.conf import settings
from django.contrib.auth.models import AbstractUser
from django.core.exceptions import ValidationError
from django.db import models, router, transaction
from django.db.models import Q
from django.db.models.functions import Lower
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from apps.core.models import BaseModel

from .managers import DERIVED_ACCESS_FIELDS, UserManager
from .phone_numbers import E164_PATTERN, normalize_phone_number, validate_e164
from .roles import sync_role_group
from .usernames import USERNAME_HELP_TEXT, USERNAME_MAX_LENGTH, USERNAME_PATTERN, normalize_username, validate_username

DUPLICATE_EMAIL_MESSAGE = _("A user with this email address already exists.")
DUPLICATE_PHONE_MESSAGE = _("A user with this phone number already exists.")
DUPLICATE_USERNAME_MESSAGE = _("A user with this username already exists.")
NEEDS_IDENTIFIER_MESSAGE = _("An account needs a mobile number, an email address or a username.")


class Role(models.TextChoices):
    SUPER_ADMIN = "SUPER_ADMIN", _("Super Administrator")
    CONTENT_MANAGER = "CONTENT_MANAGER", _("Content Manager")
    MEMBER = "MEMBER", _("Member")


class Status(models.TextChoices):
    PENDING = "PENDING", _("Pending")
    ACTIVE = "ACTIVE", _("Active")
    SUSPENDED = "SUSPENDED", _("Suspended")
    DISABLED = "DISABLED", _("Disabled")


class User(AbstractUser):
    """
    Identity invariants:

    * REG-02/REG-03: an account signs in with its mobile number, its email address or its
      username; it has at least one of them (database check). Members choose one when they
      register; the others are optional. Email is added (and verified) from the account page.
    * ``username`` is NULL or stored lowercased in the shape apps.accounts.usernames allows
      (database check), unique case-insensitively. It starts with a letter and has no @, so it
      can never be mistaken for a mobile number or an email address.
    * ``email`` is NULL or stored trimmed and lowercased, unique case-insensitively.
    * ``phone_number`` is NULL or stored in E.164 format, and unique when set, so
      0712345678 and +255712345678 can never belong to two accounts.
    * ``phone_verified`` records proven ownership of the number (by SMS, when that
      exists). It is separate from sign-in: an unverified number signs in, but it is
      never accepted as proof for recovering an account.
    * ``role`` and ``status`` are the only writable access fields. ``is_staff``,
      ``is_superuser`` and ``is_active`` are derived from them on clean() and save(),
      and database constraints reject any other combination.
    """

    Role = Role
    Status = Status

    # role -> (is_staff, is_superuser)
    ROLE_ACCESS_FLAGS = {
        Role.SUPER_ADMIN: (True, True),
        Role.CONTENT_MANAGER: (True, False),
        Role.MEMBER: (False, False),
    }

    id = models.UUIDField(
        primary_key=True,
        default=uuid.uuid4,
        editable=False,
    )

    username = None

    email = models.EmailField(
        _("email address"),
        max_length=254,
        unique=True,
        null=True,
        blank=True,
        error_messages={"unique": DUPLICATE_EMAIL_MESSAGE},
        help_text=_("Optional for members. Used for password recovery once verified."),
    )

    phone_number = models.CharField(
        _("phone number"),
        max_length=16,
        null=True,
        blank=True,
        validators=[validate_e164],
        error_messages={"unique": DUPLICATE_PHONE_MESSAGE},
        help_text=_("Optional. Stored in international format, for example +255712345678."),
    )
    username = models.CharField(
        _("username"), max_length=USERNAME_MAX_LENGTH, unique=True, null=True, blank=True,
        validators=[validate_username], error_messages={"unique": DUPLICATE_USERNAME_MESSAGE},
        help_text=USERNAME_HELP_TEXT,
    )
    phone_verified = models.BooleanField(
        _("phone verified"), default=False,
        help_text=_("Ownership of the number was proven (by SMS). Not required to sign in; "
                    "an unverified number is never used to recover an account."),
    )

    role = models.CharField(
        _("role"),
        max_length=20,
        choices=Role.choices,
        default=Role.MEMBER,
    )

    status = models.CharField(
        _("status"),
        max_length=20,
        choices=Status.choices,
        default=Status.PENDING,
    )

    # Derived access flags. Defaults match the default role (MEMBER) and status
    # (PENDING); editable=False keeps them out of every ModelForm.
    is_active = models.BooleanField(
        _("active"),
        default=False,
        editable=False,
        help_text=_("Derived from status: only ACTIVE accounts are active."),
    )
    is_staff = models.BooleanField(
        _("staff status"),
        default=False,
        editable=False,
        help_text=_("Derived from role: Super Administrators and Content Managers are staff."),
    )
    is_superuser = models.BooleanField(
        _("superuser status"),
        default=False,
        editable=False,
        help_text=_("Derived from role: only Super Administrators are superusers."),
    )

    email_verified = models.BooleanField(_("email verified"), default=False)
    # PRD-01 (from REG-04): set for members created by public sign-up from PRD-01 on (and by the
    # uncommitted REG-04 path). Their email address signs in and recovers the account only once
    # its ownership is confirmed. An email sign-up stays PENDING until the emailed link confirms
    # it; when the account also has another sign-in identifier (a mobile number or a username),
    # only its signed-in owner can confirm, so a mistyped address gives its inbox no way in.
    # False for every account created before, which keep signing in as they always have.
    email_ownership_required = models.BooleanField(
        _("email needs confirmed ownership before sign-in"), default=False, editable=False,
        help_text=_("Created by public sign-up: the email address signs in and recovers the account only after "
                    "its ownership is confirmed."),
    )

    updated_at = models.DateTimeField(_("updated at"), auto_now=True)

    USERNAME_FIELD = "email"
    REQUIRED_FIELDS = []

    objects = UserManager()

    class Meta:
        verbose_name = _("user")
        verbose_name_plural = _("users")
        ordering = ["-date_joined"]
        indexes = [
            models.Index(fields=["role"], name="accounts_user_role_idx"),
            models.Index(fields=["status"], name="accounts_user_status_idx"),
        ]
        constraints = [
            # Backstop for writes that bypass normalization (e.g. QuerySet.update()).
            models.UniqueConstraint(
                Lower("email"),
                name="accounts_user_email_ci_unique",
                violation_error_message=DUPLICATE_EMAIL_MESSAGE,
            ),
            # A constraint rather than unique=True avoids an unused LIKE index.
            # PostgreSQL treats NULLs as distinct, so many users may have no phone.
            models.UniqueConstraint(
                fields=["phone_number"],
                name="accounts_user_phone_unique",
            ),
            models.CheckConstraint(
                condition=Q(email__isnull=True) | ~Q(email=""),
                name="accounts_user_email_not_blank",
            ),
            # REG-03: as for email, a backstop for writes that bypass normalization.
            models.UniqueConstraint(
                Lower("username"),
                name="accounts_user_username_ci_unique",
                violation_error_message=DUPLICATE_USERNAME_MESSAGE,
            ),
            models.CheckConstraint(
                condition=Q(username__isnull=True) | Q(username__regex=USERNAME_PATTERN),
                name="accounts_user_username_format",
            ),
            # REG-02/REG-03: every account can sign in with something.
            models.CheckConstraint(
                condition=Q(email__isnull=False) | Q(phone_number__isnull=False) | Q(username__isnull=False),
                name="accounts_user_has_sign_in_identifier",
                violation_error_message=NEEDS_IDENTIFIER_MESSAGE,
            ),
            models.CheckConstraint(
                condition=Q(phone_number__isnull=True) | Q(phone_number__regex=E164_PATTERN),
                name="accounts_user_phone_e164",
            ),
            models.CheckConstraint(
                condition=Q(role__in=Role.values),
                name="accounts_user_role_valid",
            ),
            models.CheckConstraint(
                condition=Q(status__in=Status.values),
                name="accounts_user_status_valid",
            ),
            models.CheckConstraint(
                condition=(
                    Q(role=Role.SUPER_ADMIN, is_staff=True, is_superuser=True)
                    | Q(role=Role.CONTENT_MANAGER, is_staff=True, is_superuser=False)
                    | Q(role=Role.MEMBER, is_staff=False, is_superuser=False)
                ),
                name="accounts_user_role_matches_flags",
                violation_error_message=_("Staff and superuser flags must match the role."),
            ),
            models.CheckConstraint(
                condition=(
                    Q(status=Status.ACTIVE, is_active=True)
                    | (~Q(status=Status.ACTIVE) & Q(is_active=False))
                ),
                name="accounts_user_status_matches_active",
                violation_error_message=_("The active flag must match the account status."),
            ),
        ]

    def __str__(self):
        return self.email or self.phone_number or self.username or str(self.pk)

    @property
    def sign_in_identifier(self):
        """What the member signs in with, for display: the email if set, else the phone,
        else the username."""
        return self.email or self.phone_number or self.username or ""

    @property
    def has_verified_email(self):
        return bool(self.email and self.email_verified)

    @property
    def has_verified_contact(self):
        """PRD-01: proven ownership of at least one way to reach the member: a confirmed email
        address or a mobile number verified by SMS. A sign-in identifier alone is not proof."""
        return bool((self.email and self.email_verified) or (self.phone_number and self.phone_verified))

    @property
    def email_is_only_identifier(self):
        """PRD-03A: the account signs up and in with its email address and has nothing else."""
        return bool(self.email) and not self.phone_number and not self.username

    @property
    def email_signs_in(self):
        """May this account sign in with its email address?

        * Confirmed addresses, and accounts from before REG-04 (flag off): yes, as always.
        * PRD-03A: an address that is the account's ONLY identifier (an email sign-up): yes, at
          once, without confirmation; it is how the member chose to sign in.
        * REG-04: an unconfirmed address on an account that also has a mobile number or a username:
          no, until its owner confirms it, so whoever owns a mistyped inbox gets no way in."""
        return bool(self.email) and (self.email_verified or not self.email_ownership_required
                                     or self.email_is_only_identifier)

    @property
    def email_recovers_account(self):
        """PRD-03A: may a password-reset link be sent to this address? A confirmed address, or the
        address an email sign-up chose as its only identifier (the member signs in with it). Never a
        mobile number or a username, and never an unconfirmed address on an account that has
        another identifier. Completing a reset from the link proves the inbox and confirms it."""
        return bool(self.email) and (self.email_verified or self.email_is_only_identifier)

    def _get_session_auth_hash(self, secret=None):
        """
        RG-03: sessions are bound to the sign-in address as well as the password hash,
        so changing either ends every other session on its next request (Django
        compares this value on each request). The view that completes a change keeps
        its own session with update_session_auth_hash. SECRET_KEY_FALLBACKS still work:
        Django's fallback check calls this method with each old secret.
        """
        from django.utils.crypto import salted_hmac

        # REG-02: phone-only accounts bind to their number instead (unchanged for email accounts).
        # REG-03: accounts with neither bind to their username (unchanged for all others).
        if self.email:
            identifier = self.email
        elif self.phone_number:
            identifier = f"phone:{self.phone_number}"
        else:
            identifier = f"username:{self.username}"
        return salted_hmac("apps.accounts.models.User.session_auth_hash", f"{self.password}\x1f{identifier}",
                           secret=secret, algorithm="sha256").hexdigest()

    def sync_access_flags(self):
        """Derive is_staff, is_superuser and is_active from role and status."""
        self.is_staff, self.is_superuser = self.ROLE_ACCESS_FLAGS.get(self.role, (False, False))
        self.is_active = self.status == self.Status.ACTIVE

    def clean_fields(self, exclude=None):
        exclude = set(exclude or ())
        errors = {}

        self.email = type(self).objects.normalize_email(self.email) or None
        self.username = normalize_username(self.username)
        if "phone_number" not in exclude:
            try:
                self.phone_number = normalize_phone_number(self.phone_number)
            except ValidationError as error:
                errors["phone_number"] = error.error_list
                exclude.add("phone_number")

        try:
            super().clean_fields(exclude=exclude)
        except ValidationError as error:
            errors = error.update_error_dict(errors)

        if errors:
            raise ValidationError(errors)

    @classmethod
    def from_db(cls, db, field_names, values):
        instance = super().from_db(db, field_names, values)
        instance._loaded_email = instance.__dict__.get("email")
        instance._loaded_phone = instance.__dict__.get("phone_number")
        instance._loaded_role = instance.__dict__.get("role")
        instance._loaded_status = instance.__dict__.get("status")
        return instance

    def is_last_active_super_admin(self):
        """True if this saved account is the only ACTIVE Super Administrator."""
        if self._state.adding:
            return False
        active_super_admins = type(self)._default_manager.filter(
            role=self.Role.SUPER_ADMIN, status=self.Status.ACTIVE
        )
        return (
            active_super_admins.filter(pk=self.pk).exists()
            and not active_super_admins.exclude(pk=self.pk).exists()
        )

    def clean(self):
        super().clean()
        self.sync_access_flags()
        loaded_status = getattr(self, "_loaded_status", None)
        if (
            loaded_status not in (None, self.Status.PENDING)
            and self.status == self.Status.PENDING
        ):
            raise ValidationError(
                {"status": _("An account cannot be returned to Pending.")},
                code="invalid_transition",
            )
        remains_super_admin = self.role == self.Role.SUPER_ADMIN and self.status == self.Status.ACTIVE
        if not remains_super_admin and self.is_last_active_super_admin():
            raise ValidationError(
                _("At least one active Super Administrator must remain."),
                code="last_super_admin",
            )

    def validate_constraints(self, exclude=None):
        # Email and username uniqueness are reported once, as field errors, by
        # validate_unique() on the normalized (lowercased) values. The case-insensitive
        # indexes still enforce them in the database.
        exclude = set(exclude or ()) | {"email", "username"}
        super().validate_constraints(exclude=exclude)

    def save(self, *args, **kwargs):
        self.email = type(self).objects.normalize_email(self.email) or None
        self.phone_number = normalize_phone_number(self.phone_number)
        self.username = normalize_username(self.username)
        if not self.email and not self.phone_number and not self.username:
            raise ValueError(NEEDS_IDENTIFIER_MESSAGE)
        self.sync_access_flags()

        update_fields = kwargs.get("update_fields")
        saves_email = update_fields is None or "email" in update_fields
        saves_phone = update_fields is None or "phone_number" in update_fields
        loaded_phone = getattr(self, "_loaded_phone", None)
        phone_changed = saves_phone and not self._state.adding and self.phone_number != loaded_phone
        if phone_changed:
            # Ownership proof belongs to a number, not to the account (as for email).
            self.phone_verified = False

        loaded_email = getattr(self, "_loaded_email", None)
        email_changed = saves_email and loaded_email is not None and self.email != loaded_email
        if email_changed:
            # Verification belongs to an address, not to the account.
            self.email_verified = False

        if update_fields is not None:
            update_fields = set(update_fields)
            if update_fields & ({"role", "status"} | set(DERIVED_ACCESS_FIELDS)):
                update_fields |= set(DERIVED_ACCESS_FIELDS)
            if email_changed:
                update_fields.add("email_verified")
            if phone_changed:
                update_fields.add("phone_verified")
            kwargs["update_fields"] = update_fields

        role_changed = self._state.adding or self.role != getattr(self, "_loaded_role", None)
        with transaction.atomic(using=kwargs.get("using") or router.db_for_write(type(self))):
            super().save(*args, **kwargs)
            if role_changed and (update_fields is None or "role" in update_fields):
                sync_role_group(self)
        if saves_email:
            self._loaded_email = self.email
        if saves_phone:
            self._loaded_phone = self.phone_number
        self._loaded_role = self.role
        self._loaded_status = self.status


class PendingEmailChange(BaseModel):
    """
    A self-service email change waiting for the new inbox to confirm it. The sign-in
    address only changes on confirmation (apps.accounts.services.confirm_email_change),
    because advertiser invitations and the demo allowlist trust the address on the account.

    At most one per account. A new request, confirmation, cancellation or password change
    deletes the row, which ends its link; the audit trail keeps the history. An expired row
    is ignored and stays until the next of those.
    """

    user = models.OneToOneField(User, on_delete=models.CASCADE, related_name="pending_email_change")
    new_email = models.EmailField(_("new email address"), max_length=254)

    class Meta:
        verbose_name = _("pending email change")
        verbose_name_plural = _("pending email changes")

    def __str__(self):
        return f"{self.user_id} -> {self.new_email}"

    @property
    def is_expired(self):
        lifetime = timedelta(seconds=settings.ACCOUNTS_EMAIL_VERIFICATION_TIMEOUT)
        return timezone.now() >= self.created_at + lifetime


class PhoneVerificationChallenge(BaseModel):
    """
    PAY-LIVE-01F-B: one SMS code sent to prove ownership of a mobile number
    (apps.accounts.phone_verification).

    The code itself is never stored: only a keyed hash (HMAC-SHA256 with the site secret)
    bound to this challenge and to ``phone_number``, the exact number it was sent to. A
    challenge verifies that number only, and only while it is still the account's number.
    At most one challenge per account is ACTIVE; a new request supersedes it. Closed
    challenges are kept briefly for throttling and audit context, then deleted by a job.
    """

    class Status(models.TextChoices):
        ACTIVE = "ACTIVE", _("Waiting for the code")
        VERIFIED = "VERIFIED", _("Verified")
        EXPIRED = "EXPIRED", _("Expired")
        EXHAUSTED = "EXHAUSTED", _("Too many attempts")
        SUPERSEDED = "SUPERSEDED", _("Replaced or no longer valid")
        UNDELIVERED = "UNDELIVERED", _("Could not be sent")

    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="phone_challenges")
    phone_number = models.CharField(_("phone number"), max_length=16, validators=[validate_e164])
    code_hash = models.CharField(_("code hash"), max_length=64, editable=False)
    expires_at = models.DateTimeField(_("expires at"))
    attempts = models.PositiveSmallIntegerField(_("attempts"), default=0)
    status = models.CharField(_("status"), max_length=12, choices=Status.choices, default=Status.ACTIVE)
    closed_at = models.DateTimeField(_("closed at"), null=True, blank=True)

    class Meta:
        verbose_name = _("phone verification challenge")
        verbose_name_plural = _("phone verification challenges")
        indexes = [
            models.Index(fields=["user", "-created_at"], name="phone_challenge_user_idx"),
            models.Index(fields=["status", "expires_at"], name="phone_challenge_status_idx"),
        ]
        constraints = [
            models.CheckConstraint(
                condition=Q(status__in=["ACTIVE", "VERIFIED", "EXPIRED", "EXHAUSTED", "SUPERSEDED", "UNDELIVERED"]),
                name="phone_challenge_status_valid"),
            models.CheckConstraint(condition=Q(code_hash__regex=r"^[0-9a-f]{64}$"), name="phone_challenge_hash_hex"),
            models.CheckConstraint(condition=Q(status="ACTIVE", closed_at__isnull=True)
                                   | (~Q(status="ACTIVE") & Q(closed_at__isnull=False)),
                                   name="phone_challenge_closed_recorded"),
        ]

    def __str__(self):
        return f"{self.user_id} [{self.status}]"  # Never the number or the hash.
