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

    search_fields = ("email", "first_name", "last_name")

    readonly_fields = (
        "id",
        "date_joined",
        "last_login",
        "created_at",
        "updated_at",
    )

    fieldsets = (
        (None, {"fields": ("email", "password")}),
        (
            "Personal Information",
            {"fields": ("first_name", "last_name")},
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
                    "created_at",
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
                    "password1",
                    "password2",
                    "role",
                    "status",
                    "is_staff",
                    "is_active",
                ),
            },
        ),
    )