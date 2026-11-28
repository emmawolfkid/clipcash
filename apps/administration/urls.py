from django.urls import path

from apps.earnings import console as earnings_console
from apps.referrals import console as referrals_console
from apps.withdrawals import console as withdrawals_console
from apps.reports import views as reports
from apps.support import console as support

from . import memberships as membership_console
from . import views

app_name = "administration"

urlpatterns = [
    path("", views.OverviewView.as_view(), name="overview"),
    path("settings/", views.SettingsHubView.as_view(), name="settings"),
    path("health/", views.HealthView.as_view(), name="health"),
    path("members/", views.MemberDirectoryView.as_view(), name="members"),
    path("members/<uuid:pk>/", views.MemberDetailView.as_view(), name="member"),
    path("members/<uuid:pk>/complimentary/", views.ComplimentaryGrantView.as_view(), name="member_complimentary"),
    # PAY-LIVE-01D: suspend, reinstate, cancel or extend one of the member's memberships.
    path("members/<uuid:pk>/memberships/<uuid:subscription_id>/<str:action>/",
         membership_console.MembershipActionView.as_view(), name="member_membership_action"),
    path("security/", views.SecurityView.as_view(), name="security"),
    path("analytics/", reports.AnalyticsView.as_view(), name="analytics"),
    path("reports/", reports.ReportListView.as_view(), name="reports"),
    path("reports/<slug:slug>/", reports.ReportView.as_view(), name="report"),
    path("reports/<slug:slug>/export/", reports.ReportExportView.as_view(), name="report_export"),
    path("support/", support.QueueView.as_view(), name="support"),
    path("support/<str:reference>/", support.TicketView.as_view(), name="support_ticket"),
    path("videos/", views.VideoListView.as_view(), name="videos"),
    path("videos/new/", views.VideoCreateView.as_view(), name="video_create"),
    path("videos/<uuid:pk>/", views.VideoDetailView.as_view(), name="video_detail"),
    path("videos/storage/", views.VideoStorageView.as_view(), name="video_storage"),
    path("videos/<uuid:pk>/upload/<str:step>/", views.UploadView.as_view(), name="video_upload"),
    path("videos/<uuid:pk>/local-upload/", views.VideoLocalUploadView.as_view(), name="video_local_upload"),
    path("videos/<uuid:pk>/<str:action>/", views.VideoActionView.as_view(), name="video_action"),
    path("categories/", views.CategoryListView.as_view(), name="categories"),
    path("categories/<uuid:pk>/", views.CategoryEditView.as_view(), name="category_edit"),
    path("advertisers/", views.AdvertiserQueueView.as_view(), name="advertisers"),
    path("advertisers/new/", views.ClientOrganizationCreateView.as_view(), name="advertiser_create"),
    path("advertisers/<uuid:pk>/", views.AdvertiserReviewView.as_view(), name="advertiser_review"),
    path("campaigns/", views.CampaignQueueView.as_view(), name="campaigns"),
    path("campaigns/new/", views.StaffCampaignFormView.as_view(), name="campaign_create"),
    path("campaigns/<uuid:pk>/edit/", views.StaffCampaignFormView.as_view(), name="campaign_edit"),
    path("campaigns/<uuid:pk>/", views.CampaignReviewView.as_view(), name="campaign_review"),
    path("campaigns/<uuid:pk>/credits/", views.CampaignCreditsView.as_view(), name="campaign_credits"),
    path("viewing/", views.ViewingListView.as_view(), name="viewing"),
    path("viewing/<uuid:pk>/", views.ViewingReviewView.as_view(), name="viewing_review"),
    path("rewards/", views.DemoRewardListView.as_view(), name="demo_rewards"),
    # FIN-01: real TZS rewards and campaign funding.
    path("video-rewards/", earnings_console.VideoRewardsView.as_view(), name="video_rewards"),
    path("video-rewards/<uuid:pk>/", earnings_console.VideoRewardView.as_view(), name="video_reward"),
    path("funding/", earnings_console.FundingView.as_view(), name="campaign_funding"),
    path("funding/deposits/<uuid:pk>/", earnings_console.DepositView.as_view(), name="campaign_deposit"),
    path("earning-limits/", earnings_console.EarningRulesView.as_view(), name="earning_rules"),
    # FIN-02: real withdrawals and manual payouts.
    path("payouts/", withdrawals_console.WithdrawalRequestsView.as_view(), name="withdrawal_requests"),
    path("payouts/<uuid:pk>/", withdrawals_console.WithdrawalRequestView.as_view(), name="withdrawal_request"),
    path("payouts/settings/", withdrawals_console.WithdrawalSettingsView.as_view(), name="withdrawal_settings"),
    path("payouts/identity/", withdrawals_console.WithdrawalIdentitiesView.as_view(), name="withdrawal_identities"),
    # REF-01: referral programme.
    path("referrals/", referrals_console.ReferralListView.as_view(), name="referrals"),
    path("referrals/programme/", referrals_console.ReferralProgrammeView.as_view(), name="referral_programme"),
    path("referrals/funding/", referrals_console.ReferralFundingView.as_view(), name="referral_funding"),
    path("referrals/<uuid:pk>/", referrals_console.ReferralDetailView.as_view(), name="referral"),
    path("rewards/policies/", views.RewardPolicyListView.as_view(), name="reward_policies"),
    path("rewards/policies/<uuid:pk>/", views.RewardPolicyView.as_view(), name="reward_policy"),
    path("payments/", views.PaymentListView.as_view(), name="payments"),
    path("payments/exceptions/", views.PaymentExceptionsView.as_view(), name="payment_exceptions"),
    path("payments/funding/", views.PaymentFundingView.as_view(), name="payment_funding"),
    path("payments/<uuid:pk>/", views.PaymentDetailView.as_view(), name="payment"),
    path("plans/", views.PlanListView.as_view(), name="plans"),
    path("plans/new/", views.PlanCreateView.as_view(), name="plan_create"),
    path("plans/<uuid:pk>/", views.PlanEditView.as_view(), name="plan"),
    path("membership-payments/", views.MembershipPaymentQueueView.as_view(), name="membership_payments"),
    path("membership-payments/instructions/", views.PaymentInstructionsView.as_view(), name="payment_instructions"),
    path("membership-payments/instructions/new/", views.PaymentMethodFormView.as_view(), name="payment_method_new"),
    path("membership-payments/instructions/<uuid:pk>/edit/", views.PaymentMethodFormView.as_view(),
         name="payment_method_edit"),
    path("membership-payments/instructions/<uuid:pk>/retire/",
         views.PaymentMethodStatusView.as_view(action="retire"), name="payment_method_retire"),
    path("membership-payments/instructions/<uuid:pk>/reactivate/",
         views.PaymentMethodStatusView.as_view(action="reactivate"), name="payment_method_reactivate"),
    path("membership-payments/<uuid:pk>/", views.MembershipPaymentDetailView.as_view(), name="membership_payment"),
    path("wallets/", views.WalletListView.as_view(), name="wallets"),
    path("wallets/reconciliation/", views.WalletReconciliationView.as_view(), name="wallet_reconciliation"),
    path("wallets/<uuid:pk>/", views.WalletDetailView.as_view(), name="wallet"),
    path("withdrawals/", views.WithdrawalListView.as_view(), name="withdrawals"),
    path("withdrawals/exceptions/", views.WithdrawalExceptionsView.as_view(), name="withdrawal_exceptions"),
    path("withdrawals/beneficiaries/", views.BeneficiaryListView.as_view(), name="beneficiaries"),
    path("withdrawals/<uuid:pk>/", views.WithdrawalDetailView.as_view(), name="withdrawal"),
]
