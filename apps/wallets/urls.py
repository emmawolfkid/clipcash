from django.urls import path

from . import views

app_name = "wallets"

urlpatterns = [
    path("", views.WalletView.as_view(), name="wallet"),
    path("transactions/<uuid:transaction_id>/", views.WalletTransactionView.as_view(), name="transaction"),
]
