from django.contrib import admin
from django.contrib.auth.admin import UserAdmin as BaseUserAdmin

from .models import User


@admin.register(User)
class UserAdmin(BaseUserAdmin):
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