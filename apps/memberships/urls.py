from django.urls import path

from . import views

app_name = "memberships"

urlpatterns = [
    path("", views.PlanListView.as_view(), name="plans"),
    path("plans/<slug:code>/", views.PlanDetailView.as_view(), name="plan_detail"),
    path("plans/<slug:code>/choose/", views.ChoosePlanView.as_view(), name="choose"),
    path("mine/", views.MyMembershipView.as_view(), name="mine"),
    path("mine/<uuid:pk>/", views.SubscriptionDetailView.as_view(), name="subscription_detail"),
    path("mine/<uuid:pk>/cancel/", views.CancelPendingView.as_view(), name="cancel_pending"),
]
