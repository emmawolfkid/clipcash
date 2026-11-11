from django.urls import path

from . import views

app_name = "payments"

urlpatterns = [
    path("", views.MemberPaymentsView.as_view(), name="history"),
    path("membership/<uuid:subscription_id>/start/", views.StartMembershipPaymentView.as_view(),
         name="start_membership"),
    path("intent/<uuid:intent_id>/", views.IntentView.as_view(), name="intent"),
    path("advertisers/<uuid:organization_id>/funding/", views.AdvertiserFundingView.as_view(), name="funding"),
    path("callback/sandbox/", views.SandboxCallbackView.as_view(), name="sandbox_callback"),
    # PAY-01: manual membership payments (Airtel Lipa).
    path("membership/<uuid:subscription_id>/pay/", views.ManualPayView.as_view(), name="manual_pay"),
    path("membership/submissions/<uuid:pk>/", views.ManualClaimView.as_view(), name="manual_claim"),
]
