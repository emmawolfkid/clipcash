from django.urls import path

from apps.reports import views as reports
from apps.support import console as support

from . import views

app_name = "administration"

urlpatterns = [
    path("", views.OverviewView.as_view(), name="overview"),
    path("health/", views.HealthView.as_view(), name="health"),
    path("members/", views.MemberDirectoryView.as_view(), name="members"),
    path("members/<uuid:pk>/", views.MemberDetailView.as_view(), name="member"),
    path("members/<uuid:pk>/complimentary/", views.ComplimentaryGrantView.as_view(), name="member_complimentary"),
    path("security/", views.SecurityView.as_view(), name="security"),
    path("reports/", reports.ReportListView.as_view(), name="reports"),
    path("reports/<slug:slug>/", reports.ReportView.as_view(), name="report"),
    path("reports/<slug:slug>/export/", reports.ReportExportView.as_view(), name="report_export"),
    path("support/", support.QueueView.as_view(), name="support"),
    path("support/<str:reference>/", support.TicketView.as_view(), name="support_ticket"),
    path("videos/", views.VideoListView.as_view(), name="videos"),
    path("videos/new/", views.VideoCreateView.as_view(), name="video_create"),
    path("videos/<uuid:pk>/", views.VideoDetailView.as_view(), name="video_detail"),
    path("videos/<uuid:pk>/upload/<str:step>/", views.UploadView.as_view(), name="video_upload"),
    path("videos/<uuid:pk>/<str:action>/", views.VideoActionView.as_view(), name="video_action"),
    path("categories/", views.CategoryListView.as_view(), name="categories"),
    path("categories/<uuid:pk>/", views.CategoryEditView.as_view(), name="category_edit"),
    path("advertisers/", views.AdvertiserQueueView.as_view(), name="advertisers"),
    path("advertisers/<uuid:pk>/", views.AdvertiserReviewView.as_view(), name="advertiser_review"),
    path("campaigns/", views.CampaignQueueView.as_view(), name="campaigns"),
    path("campaigns/<uuid:pk>/", views.CampaignReviewView.as_view(), name="campaign_review"),
    path("viewing/", views.ViewingListView.as_view(), name="viewing"),
    path("viewing/<uuid:pk>/", views.ViewingReviewView.as_view(), name="viewing_review"),
    path("rewards/", views.DemoRewardListView.as_view(), name="demo_rewards"),
    path("rewards/policies/", views.RewardPolicyListView.as_view(), name="reward_policies"),
    path("rewards/policies/<uuid:pk>/", views.RewardPolicyView.as_view(), name="reward_policy"),
    path("payments/", views.PaymentListView.as_view(), name="payments"),
    path("payments/exceptions/", views.PaymentExceptionsView.as_view(), name="payment_exceptions"),
    path("payments/funding/", views.PaymentFundingView.as_view(), name="payment_funding"),
    path("payments/<uuid:pk>/", views.PaymentDetailView.as_view(), name="payment"),
    path("wallets/", views.WalletListView.as_view(), name="wallets"),
    path("wallets/reconciliation/", views.WalletReconciliationView.as_view(), name="wallet_reconciliation"),
    path("wallets/<uuid:pk>/", views.WalletDetailView.as_view(), name="wallet"),
    path("withdrawals/", views.WithdrawalListView.as_view(), name="withdrawals"),
    path("withdrawals/exceptions/", views.WithdrawalExceptionsView.as_view(), name="withdrawal_exceptions"),
    path("withdrawals/beneficiaries/", views.BeneficiaryListView.as_view(), name="beneficiaries"),
    path("withdrawals/<uuid:pk>/", views.WithdrawalDetailView.as_view(), name="withdrawal"),
]
