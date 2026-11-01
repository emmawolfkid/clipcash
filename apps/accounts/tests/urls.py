"""URLconf for access-control tests: the project URLs plus protected probe views."""

from django.http import HttpResponse
from django.urls import include, path
from django.views import View

from apps.accounts.access import RoleRequiredMixin, role_required, verified_account_required
from apps.accounts.models import User


def ok(request):
    return HttpResponse("ok")


class SuperAdminOnlyView(RoleRequiredMixin, View):
    allowed_roles = (User.Role.SUPER_ADMIN,)

    def get(self, request):
        return HttpResponse("ok")


urlpatterns = [
    path("probe/member/", verified_account_required(ok), name="probe_member"),
    path(
        "probe/staff/",
        role_required(User.Role.CONTENT_MANAGER, User.Role.SUPER_ADMIN)(ok),
        name="probe_staff",
    ),
    path(
        "probe/staff-unverified-ok/",
        role_required(User.Role.CONTENT_MANAGER, require_verified_email=False)(ok),
        name="probe_staff_unverified",
    ),
    path("probe/super-admin/", SuperAdminOnlyView.as_view(), name="probe_super_admin"),
    path("", include("config.urls")),
]
