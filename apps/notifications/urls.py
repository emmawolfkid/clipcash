from django.urls import path

from . import views

app_name = "notifications"

urlpatterns = [
    path("", views.InboxView.as_view(), name="inbox"),
    path("read-all/", views.MarkAllReadView.as_view(), name="read_all"),
    path("<uuid:notification_id>/open/", views.OpenView.as_view(), name="open"),
    path("<uuid:notification_id>/read/", views.MarkReadView.as_view(), name="read"),
]
