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
]
