from django.contrib import admin
from django.urls import include, path

urlpatterns = [
    path("admin/", admin.site.urls),
    path("accounts/", include("apps.accounts.urls")),
    path("memberships/", include("apps.memberships.urls")),
    path("i18n/", include("django.conf.urls.i18n")),
]
