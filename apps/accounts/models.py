import uuid

from django.contrib.auth.models import AbstractUser
from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import Q
from django.db.models.functions import Lower
from django.utils.translation import gettext_lazy as _

from .managers import DERIVED_ACCESS_FIELDS, UserManager
from .phone_numbers import E164_PATTERN, normalize_phone_number, validate_e164

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

    * ``email`` is stored trimmed and lowercased, and is unique case-insensitively.
    * ``phone_number`` is optional, stored in E.164 format or NULL, and unique when set.
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
        error_messages={"unique": DUPLICATE_EMAIL_MESSAGE},
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
                condition=~Q(email=""),
                name="accounts_user_email_not_blank",
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
        return self.email

    def sync_access_flags(self):
        """Derive is_staff, is_superuser and is_active from role and status."""
        self.is_staff, self.is_superuser = self.ROLE_ACCESS_FLAGS.get(self.role, (False, False))
        self.is_active = self.status == self.Status.ACTIVE

    def clean_fields(self, exclude=None):
        exclude = set(exclude or ())
        errors = {}

        self.email = type(self).objects.normalize_email(self.email)
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

    def clean(self):
        super().clean()
        self.sync_access_flags()

    def validate_constraints(self, exclude=None):
        # Email uniqueness is reported once, as a field error, by validate_unique() on
        # the normalized address. The case-insensitive index still enforces it in the
        # database, and blank emails are rejected by field validation.
        exclude = set(exclude or ()) | {"email"}
        super().validate_constraints(exclude=exclude)

    def save(self, *args, **kwargs):
        self.email = type(self).objects.normalize_email(self.email)
        if not self.email:
            raise ValueError(_("An email address is required."))
        self.phone_number = normalize_phone_number(self.phone_number)
        self.sync_access_flags()

        update_fields = kwargs.get("update_fields")
        if update_fields is not None:
            update_fields = set(update_fields)
            if update_fields & ({"role", "status"} | set(DERIVED_ACCESS_FIELDS)):
                update_fields |= set(DERIVED_ACCESS_FIELDS)
            kwargs["update_fields"] = update_fields

        super().save(*args, **kwargs)
