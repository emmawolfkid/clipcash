from django.urls import path

from . import views

app_name = "withdrawals"

urlpatterns = [
    path("", views.OverviewView.as_view(), name="overview"),
    path("destinations/", views.DestinationsView.as_view(), name="destinations"),
    path("request/", views.RequestView.as_view(), name="request_new"),
    path("requests/<uuid:withdrawal_id>/", views.RequestDetailView.as_view(), name="request"),
    path("identity/", views.IdentityView.as_view(), name="identity"),
    path("demo/<uuid:withdrawal_id>/", views.DemoDetailView.as_view(), name="demo"),
    path("callback/sandbox/", views.SandboxPayoutCallbackView.as_view(), name="sandbox_callback"),
]
