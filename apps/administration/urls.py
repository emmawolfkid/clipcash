from django.urls import path

from . import views

app_name = "administration"

urlpatterns = [
    path("", views.OverviewView.as_view(), name="overview"),
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
]
