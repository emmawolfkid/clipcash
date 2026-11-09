from django.urls import path

from . import views

app_name = "accounts"

urlpatterns = [
    path("", views.ProfileView.as_view(), name="profile"),
    path("register/", views.RegisterView.as_view(), name="register"),
    path("register/done/", views.RegisterDoneView.as_view(), name="register_done"),
    path("activate/<uidb64>/<token>/", views.ActivateAccountView.as_view(), name="activate"),
    path("activate/complete/", views.ActivationCompleteView.as_view(), name="activation_complete"),
    path("confirm-email/<uidb64>/<token>/", views.ConfirmEmailView.as_view(), name="confirm_email"),
    path("verify/required/", views.VerificationRequiredView.as_view(), name="verification_required"),
    path("verify/resend/", views.ResendVerificationView.as_view(), name="resend_verification"),
    path(
        "verify/resend/done/",
        views.ResendVerificationDoneView.as_view(),
        name="resend_verification_done",
    ),
    path("login/", views.LoginView.as_view(), name="login"),
    path("logout/", views.LogoutView.as_view(), name="logout"),
    path("unavailable/", views.AccountUnavailableView.as_view(), name="account_unavailable"),
    path("password-reset/", views.PasswordResetView.as_view(), name="password_reset"),
    path(
        "password-reset/done/",
        views.PasswordResetDoneView.as_view(),
        name="password_reset_done",
    ),
    path(
        "reset/<uidb64>/<token>/",
        views.PasswordResetConfirmView.as_view(),
        name="password_reset_confirm",
    ),
    path(
        "reset/complete/",
        views.PasswordResetCompleteView.as_view(),
        name="password_reset_complete",
    ),
    path("password-change/", views.PasswordChangeView.as_view(), name="password_change"),
    path(
        "password-change/done/",
        views.PasswordChangeDoneView.as_view(),
        name="password_change_done",
    ),
    path("profile/edit/", views.ProfileEditView.as_view(), name="profile_edit"),
    path("email-change/", views.EmailChangeRequestView.as_view(), name="email_change"),
    path("email-change/cancel/", views.EmailChangeCancelView.as_view(), name="email_change_cancel"),
    # RG-03: the emailed link only moves its token into the session; the change is
    # completed on the token-free review page by the signed-in owner.
    path("email-change/<uuid:pk>/confirm/", views.EmailChangeReviewView.as_view(), name="email_change_review"),
    path("email-change/<uuid:pk>/<token>/", views.EmailChangeLinkView.as_view(), name="email_change_confirm"),
]
