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

DUPLICATE_EMAIL_MESSAGE = _("A user with this email address already exists.")
DUPLICATE_PHONE_MESSAGE = _("A user with this phone number already exists.")


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

    * REG-02: an account signs in with its mobile number or its email address; it has
      at least one of them (database check). Members register with a mobile number;
      email is optional and is added (and verified) from the account page.
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
            # REG-02: every account can sign in with something.
            models.CheckConstraint(
                condition=Q(email__isnull=False) | Q(phone_number__isnull=False),
                name="accounts_user_has_sign_in_identifier",
                violation_error_message=_("An account needs a mobile number or an email address."),
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
        return self.email or self.phone_number or str(self.pk)

    @property
    def sign_in_identifier(self):
        """What the member signs in with, for display: the email if set, else the phone."""
        return self.email or self.phone_number or ""

    @property
    def has_verified_email(self):
        return bool(self.email and self.email_verified)

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
        identifier = self.email if self.email else f"phone:{self.phone_number}"
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
        # Email uniqueness is reported once, as a field error, by validate_unique() on
        # the normalized address. The case-insensitive index still enforces it in the
        # database, and blank emails are rejected by field validation.
        exclude = set(exclude or ()) | {"email"}
        super().validate_constraints(exclude=exclude)

    def save(self, *args, **kwargs):
        self.email = type(self).objects.normalize_email(self.email) or None
        self.phone_number = normalize_phone_number(self.phone_number)
        if not self.email and not self.phone_number:
            raise ValueError(_("An account needs a mobile number or an email address."))
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
