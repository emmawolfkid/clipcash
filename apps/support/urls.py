from django.urls import path

from . import views

app_name = "support"

urlpatterns = [
    path("", views.TicketListView.as_view(), name="tickets"),
    path("new/", views.TicketCreateView.as_view(), name="create"),
    path("<str:reference>/", views.TicketDetailView.as_view(), name="ticket"),
]
