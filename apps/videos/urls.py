from django.urls import path

from . import media, views

app_name = "videos"

urlpatterns = [
    path("", views.LibraryView.as_view(), name="library"),
    # OPS-01: protected local video files (signed, viewer-bound, access re-checked per request).
    path("media/<str:video_id>/", media.media, name="media"),
    path("media/<str:video_id>/thumbnail/", media.thumbnail, name="media_thumbnail"),
    path("category/<slug:slug>/", views.LibraryView.as_view(), name="category"),
    path("<slug:slug>/", views.VideoDetailView.as_view(), name="detail"),
]
