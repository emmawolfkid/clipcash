from django.contrib import admin
from django.urls import include, path

from apps.core.views import MemberDashboardView

urlpatterns = [
    path("admin/", admin.site.urls),
    path("accounts/", include("apps.accounts.urls")),
    path("memberships/", include("apps.memberships.urls")),
    path("videos/", include("apps.videos.urls")),
    path("manage/", include("apps.administration.urls")),
    path("dashboard/", MemberDashboardView.as_view(), name="dashboard"),
    path("i18n/", include("django.conf.urls.i18n")),
]
