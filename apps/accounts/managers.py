from django.contrib.auth.models import BaseUserManager
from django.utils.translation import gettext_lazy as _

# Flags derived from role and status; callers must never set them directly.
DERIVED_ACCESS_FIELDS = ("is_active", "is_staff", "is_superuser")


class UserManager(BaseUserManager):
    use_in_migrations = True

    @classmethod
    def normalize_email(cls, email):
        """Trim surrounding whitespace and lowercase the whole address."""
        if email is None:
            return ""
        return str(email).strip().lower()

    def get_by_natural_key(self, email):
        """Case-insensitive lookup used by authentication and createsuperuser."""
        return self.get(email=self.normalize_email(email))

    def _create_user(self, email, password, **extra_fields):
        email = self.normalize_email(email)
        if not email:
            raise ValueError(_("An email address is required."))

        supplied_flags = sorted(set(extra_fields) & set(DERIVED_ACCESS_FIELDS))
        if supplied_flags:
            raise ValueError(
                _("%(fields)s are derived from role and status and cannot be set directly.")
                % {"fields": ", ".join(supplied_flags)}
            )

        user = self.model(email=email, **extra_fields)
        user.set_password(password)
        user.full_clean()
        user.save(using=self._db)
        return user

    def create_user(self, email, password=None, **extra_fields):
        role = extra_fields.setdefault("role", self.model.Role.MEMBER)
        if role == self.model.Role.SUPER_ADMIN:
            raise ValueError(_("Super Administrators must be created with create_superuser()."))
        return self._create_user(email, password, **extra_fields)

    def create_superuser(self, email, password=None, **extra_fields):
        # Accept the flags Django's tooling may pass, but only with consistent values.
        for flag in DERIVED_ACCESS_FIELDS:
            if extra_fields.pop(flag, True) is not True:
                raise ValueError(_("Superuser must have %(flag)s=True.") % {"flag": flag})

        extra_fields.setdefault("role", self.model.Role.SUPER_ADMIN)
        extra_fields.setdefault("status", self.model.Status.ACTIVE)

        if extra_fields["role"] != self.model.Role.SUPER_ADMIN:
            raise ValueError(_("Superuser must have the Super Administrator role."))
        if extra_fields["status"] != self.model.Status.ACTIVE:
            raise ValueError(_("Superuser must have an active account."))

        return self._create_user(email, password, **extra_fields)
