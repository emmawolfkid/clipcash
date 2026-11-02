from django.urls import path

from . import views

app_name = "withdrawals"

urlpatterns = [
    path("", views.OverviewView.as_view(), name="overview"),
    path("destinations/", views.DestinationsView.as_view(), name="destinations"),
    path("demo/<uuid:withdrawal_id>/", views.DemoDetailView.as_view(), name="demo"),
    path("callback/sandbox/", views.SandboxPayoutCallbackView.as_view(), name="sandbox_callback"),
]
