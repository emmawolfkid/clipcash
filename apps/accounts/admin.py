from django.contrib import admin
from django.contrib.auth.admin import GroupAdmin as BaseGroupAdmin
from django.contrib.auth.admin import UserAdmin as BaseUserAdmin
from django.contrib.auth.models import Group
from django.core.exceptions import PermissionDenied

from . import services
from .forms import AccessChangeFormMixin, AdminLoginForm
from .models import User

# Rate-limit admin sign-in the same way as the public login.
admin.site.login_form = AdminLoginForm

# Fields only a superuser may change: they grant or remove privileges.
PRIVILEGE_FIELDS = ("role", "groups", "user_permissions", "email_verified")


@admin.register(User)
class UserAdmin(BaseUserAdmin):
    """
    Privilege rules for non-superuser staff:

    * may only add, change or delete MEMBER accounts;
    * new accounts are always MEMBERs; role, groups, permissions and email
      verification are read-only;
    * may not change their own account here.

    Nobody may delete their own account or the last active Super Administrator.
    """

    ordering = ("-date_joined",)

    list_display = (
        "email",
        "role",
        "status",
        "email_verified",
        "is_staff",
        "is_active",
        "date_joined",
    )

    list_filter = (
        "role",
        "status",
        "email_verified",
        "is_staff",
        "is_active",
    )

    search_fields = ("email", "phone_number", "first_name", "last_name")

    # Access flags are derived from role and status by the model.
    readonly_fields = (
        "id",
        "is_active",
        "is_staff",
        "is_superuser",
        "date_joined",
        "last_login",
        "updated_at",
    )

    fieldsets = (
        (None, {"fields": ("email", "password")}),
        (
            "Personal Information",
            {"fields": ("first_name", "last_name", "phone_number")},
        ),
        (
            "ClipCash Account",
            {"fields": ("id", "role", "status", "email_verified")},
        ),
        (
            "Permissions",
            {
                "fields": (
                    "is_active",
                    "is_staff",
                    "is_superuser",
                    "groups",
                    "user_permissions",
                )
            },
        ),
        (
            "Important Dates",
            {
                "fields": (
                    "last_login",
                    "date_joined",
                    "updated_at",
                )
            },
        ),
    )

    add_fieldsets = (
        (
            None,
            {
                "classes": ("wide",),
                "fields": (
                    "email",
                    "phone_number",
                    "password1",
                    "password2",
                    "role",
                    "status",
                ),
            },
        ),
    )

    def get_readonly_fields(self, request, obj=None):
        readonly = list(super().get_readonly_fields(request, obj))
        if not request.user.is_superuser:
            readonly += [field for field in PRIVILEGE_FIELDS if field not in readonly]
        return readonly

    def _may_manage(self, request, obj):
        if obj is None or request.user.is_superuser:
            return True
        return obj.role == User.Role.MEMBER and obj.pk != request.user.pk

    def has_change_permission(self, request, obj=None):
        return super().has_change_permission(request, obj) and self._may_manage(request, obj)

    def has_delete_permission(self, request, obj=None):
        if not super().has_delete_permission(request, obj):
            return False
        if obj is None:
            return True
        if obj.pk == request.user.pk or obj.is_last_active_super_admin():
            return False
        return self._may_manage(request, obj)

    def get_actions(self, request):
        # Bulk delete bypasses per-object checks.
        actions = super().get_actions(request)
        actions.pop("delete_selected", None)
        return actions

    def get_form(self, request, obj=None, **kwargs):
        # Authorization is enforced in the form's clean(), not only by hiding fields.
        form_class = super().get_form(request, obj, **kwargs)
        return type(
            form_class.__name__,
            (AccessChangeFormMixin, form_class),
            {"actor": request.user},
        )

    def save_model(self, request, obj, form, change):
        if not change and not request.user.is_superuser:
            # Defence in depth: role is already read-only and validated.
            obj.role = User.Role.MEMBER
        old_role = getattr(obj, "_loaded_role", None)
        old_status = getattr(obj, "_loaded_status", None)
        super().save_model(request, obj, form, change)
        if change:
            services.log_access_change(request.user, obj, old_role=old_role, old_status=old_status)
        else:
            services.security_log.warning(
                "account_created_by_staff user=%s actor=%s role=%s", obj.pk, request.user.pk, obj.role
            )

    def delete_model(self, request, obj):
        # Re-check under lock: concurrent deletions must not remove the last one.
        services.lock_active_super_admins()
        if obj.pk == request.user.pk or obj.is_last_active_super_admin():
            raise PermissionDenied
        services.security_log.warning("account_deleted user=%s actor=%s", obj.pk, request.user.pk)
        super().delete_model(request, obj)


admin.site.unregister(Group)


@admin.register(Group)
class GroupAdmin(BaseGroupAdmin):
    """Groups carry permissions, so only superusers may view or edit them."""

    def has_module_permission(self, request):
        return request.user.is_superuser

    def has_view_permission(self, request, obj=None):
        return request.user.is_superuser

    def has_add_permission(self, request):
        return request.user.is_superuser

    def has_change_permission(self, request, obj=None):
        return request.user.is_superuser

    def has_delete_permission(self, request, obj=None):
        return request.user.is_superuser
