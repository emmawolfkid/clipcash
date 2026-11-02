from django.urls import path

from . import views

app_name = "rewards"

urlpatterns = [
    path("", views.DemoRewardHistoryView.as_view(), name="history"),
]
