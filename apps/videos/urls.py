from django.urls import path

from . import views

app_name = "videos"

urlpatterns = [
    path("", views.LibraryView.as_view(), name="library"),
    path("category/<slug:slug>/", views.LibraryView.as_view(), name="category"),
    path("<slug:slug>/", views.VideoDetailView.as_view(), name="detail"),
]
