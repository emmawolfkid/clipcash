from django.urls import path

from . import views

app_name = "advertising"

urlpatterns = [
    path("", views.HomeView.as_view(), name="home"),
    path("new/", views.OnboardView.as_view(), name="onboard"),
    path("invitations/<uuid:invitation_id>/<str:response>/", views.InvitationResponseView.as_view(),
         name="invitation"),
    path("<uuid:organization_id>/", views.OrganizationView.as_view(), name="organization"),
    path("<uuid:organization_id>/profile/", views.ProfileView.as_view(), name="profile"),
    path("<uuid:organization_id>/team/", views.TeamView.as_view(), name="team"),
    path("<uuid:organization_id>/team/<str:change>/<uuid:item_id>/", views.TeamChangeView.as_view(),
         name="team_change"),
    path("<uuid:organization_id>/campaigns/", views.CampaignListView.as_view(), name="campaigns"),
    path("<uuid:organization_id>/campaigns/new/", views.CampaignCreateView.as_view(), name="campaign_create"),
    path("<uuid:organization_id>/campaigns/<uuid:campaign_id>/", views.CampaignDetailView.as_view(),
         name="campaign"),
    path("<uuid:organization_id>/campaigns/<uuid:campaign_id>/edit/", views.CampaignEditView.as_view(),
         name="campaign_edit"),
    path("<uuid:organization_id>/campaigns/<uuid:campaign_id>/<str:action>/", views.CampaignActionView.as_view(),
         name="campaign_action"),
]
