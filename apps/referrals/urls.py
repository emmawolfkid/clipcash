from django.urls import path

from . import views

app_name = "referrals"

urlpatterns = [
    path("", views.ReferralOverviewView.as_view(), name="overview"),
    path("join/<str:code>/", views.JoinView.as_view(), name="join"),
]
