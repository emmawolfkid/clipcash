from django.urls import path

from . import views

app_name = "viewing"

urlpatterns = [
    path("", views.WatchListView.as_view(), name="list"),
    path("history/", views.HistoryView.as_view(), name="history"),
    path("campaign/<uuid:campaign_id>/", views.CampaignWatchView.as_view(), name="campaign"),
    path("session/<uuid:session_id>/", views.SessionView.as_view(), name="session"),
    path("session/<uuid:session_id>/events/", views.SessionEventView.as_view(), name="events"),
    path("session/<uuid:session_id>/finish/", views.SessionFinishView.as_view(), name="finish"),
]
