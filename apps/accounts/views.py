import logging

from django.contrib import messages
from django.contrib.auth import get_user_model
from django.contrib.auth import views as auth_views
from django.contrib.auth.mixins import LoginRequiredMixin
from django.contrib.auth.views import INTERNAL_RESET_SESSION_TOKEN
from django.http import HttpResponseRedirect
from django.shortcuts import redirect, render
from django.urls import reverse_lazy
from django.utils.translation import gettext_lazy as _
from django.views import View
from django.views.generic import FormView, TemplateView

from . import services
from .access import VerifiedAccountRequiredMixin
from .forms import AccountPasswordResetForm, EmailRequestForm, LoginForm, RegistrationForm
from .throttling import (
    EMAIL_REQUESTS_PER_ACCOUNT,
    EMAIL_REQUESTS_PER_IP,
    REGISTRATIONS_PER_IP,
    client_ip,
)
from .tokens import password_reset_token_generator, verification_token_generator

security_log = logging.getLogger("clipcash.security")

User = get_user_model()

TOO_MANY_REQUESTS = _("Too many requests. Please wait a while and try again.")
INVALID_LINK_TEMPLATE = "accounts/link_invalid.html"


def _email_request_allowed(request, email):
    """Throttle outgoing account emails per IP and per address."""
    ip = client_ip(request)
    email = User.objects.normalize_email(email)
    if EMAIL_REQUESTS_PER_IP.is_limited(ip) or EMAIL_REQUESTS_PER_ACCOUNT.is_limited(email):
        security_log.warning("email_request_throttled ip=%s", ip)
        return False
    EMAIL_REQUESTS_PER_IP.hit(ip)
    EMAIL_REQUESTS_PER_ACCOUNT.hit(email)
    return True


class AnonymousOnlyMixin:
    def dispatch(self, request, *args, **kwargs):
        if request.user.is_authenticated:
            return redirect("accounts:profile")
        return super().dispatch(request, *args, **kwargs)


# --- Registration and verification ---------------------------------------------


class RegisterView(AnonymousOnlyMixin, FormView):
    template_name = "accounts/register.html"
    form_class = RegistrationForm
    success_url = reverse_lazy("accounts:register_done")

    def form_valid(self, form):
        ip = client_ip(self.request)
        if REGISTRATIONS_PER_IP.is_limited(ip):
            security_log.warning("registration_throttled ip=%s", ip)
            form.add_error(None, TOO_MANY_REQUESTS)
            return self.form_invalid(form)
        REGISTRATIONS_PER_IP.hit(ip)
        email = form.cleaned_data["email"]
        # Same response whether or not mail is sent, so the per-address limit
        # cannot reveal registered addresses either.
        if _email_request_allowed(self.request, email):
            services.register(email=email, request=self.request)
        return super().form_valid(form)


class RegisterDoneView(TemplateView):
    template_name = "accounts/register_done.html"


class ActivateAccountView(auth_views.PasswordResetConfirmView):
    """
    The emailed activation link. Like Django's reset flow, the token is moved into
    the session and the URL replaced, so it never appears in later requests.
    The account becomes ACTIVE only when the owner sets a password here.
    """

    template_name = "accounts/activate_account.html"
    token_generator = verification_token_generator
    success_url = reverse_lazy("accounts:activation_complete")
    post_reset_login = False

    def get_user(self, uidb64):
        user = super().get_user(uidb64)
        return user if user is not None and user.status == User.Status.PENDING else None

    def form_valid(self, form):
        token = self.request.session.pop(INTERNAL_RESET_SESSION_TOKEN, None)
        user = services.activate_account(self.user.pk, token, form.cleaned_data["new_password1"])
        if user is None:
            return render(self.request, INVALID_LINK_TEMPLATE, status=400)
        return HttpResponseRedirect(self.get_success_url())


class ActivationCompleteView(TemplateView):
    template_name = "accounts/activation_complete.html"


class ConfirmEmailView(View):
    """Email confirmation for ACTIVE accounts. GET only shows a button, so link
    scanners cannot confirm; POST confirms once."""

    template_name = "accounts/confirm_email.html"

    def _user(self, uidb64, token):
        user = auth_views.PasswordResetConfirmView().get_user(uidb64)
        if (
            user is None
            or user.status != User.Status.ACTIVE
            or user.email_verified
            or not verification_token_generator.check_token(user, token)
        ):
            return None
        return user

    def get(self, request, uidb64, token):
        if self._user(uidb64, token) is None:
            return render(request, INVALID_LINK_TEMPLATE, status=400)
        return render(request, self.template_name)

    def post(self, request, uidb64, token):
        user = self._user(uidb64, token)
        if user is None or services.confirm_email(user.pk, token) is None:
            return render(request, INVALID_LINK_TEMPLATE, status=400)
        return render(request, "accounts/confirm_email_complete.html")


class ResendVerificationView(FormView):
    template_name = "accounts/resend_verification.html"
    form_class = EmailRequestForm
    success_url = reverse_lazy("accounts:resend_verification_done")

    def post(self, request, *args, **kwargs):
        # Signed-in unverified users resend to their own address; no form needed.
        if request.user.is_authenticated:
            if not request.user.email_verified and _email_request_allowed(
                request, request.user.email
            ):
                services.send_verification_email(request.user, request)
            messages.info(request, _("If your email still needs verification, a new link is on its way."))
            return redirect("accounts:verification_required")
        return super().post(request, *args, **kwargs)

    def form_valid(self, form):
        email = form.cleaned_data["email"]
        if _email_request_allowed(self.request, email):
            services.request_verification_email(email, self.request)
        return super().form_valid(form)


class ResendVerificationDoneView(TemplateView):
    template_name = "accounts/resend_verification_done.html"


class VerificationRequiredView(LoginRequiredMixin, TemplateView):
    template_name = "accounts/verification_required.html"

    def dispatch(self, request, *args, **kwargs):
        if request.user.is_authenticated and request.user.email_verified:
            return redirect("accounts:profile")
        return super().dispatch(request, *args, **kwargs)


# --- Sign in and out ------------------------------------------------------------


class LoginView(auth_views.LoginView):
    template_name = "accounts/login.html"
    authentication_form = LoginForm
    redirect_authenticated_user = True


class LogoutView(auth_views.LogoutView):
    """GET shows a confirmation page; only POST (with CSRF token) signs out."""

    http_method_names = ["get", "post", "options"]
    template_name = "accounts/logout_confirm.html"

    def get(self, request, *args, **kwargs):
        if not request.user.is_authenticated:
            return redirect("accounts:login")
        return render(request, self.template_name)

    def post(self, request, *args, **kwargs):
        if request.user.is_authenticated:
            security_log.info("logout user=%s", request.user.pk)
        return super().post(request, *args, **kwargs)


class AccountUnavailableView(TemplateView):
    template_name = "accounts/account_unavailable.html"


# --- Passwords ------------------------------------------------------------------


class PasswordResetView(auth_views.PasswordResetView):
    template_name = "accounts/password_reset_form.html"
    form_class = AccountPasswordResetForm
    email_template_name = "accounts/email/password_reset_body.txt"
    subject_template_name = "accounts/email/password_reset_subject.txt"
    token_generator = password_reset_token_generator
    success_url = reverse_lazy("accounts:password_reset_done")

    def form_valid(self, form):
        if not _email_request_allowed(self.request, form.cleaned_data["email"]):
            return redirect(self.success_url)  # Same response; nothing is sent.
        security_log.info("password_reset_requested ip=%s", client_ip(self.request))
        return super().form_valid(form)


class PasswordResetDoneView(auth_views.PasswordResetDoneView):
    template_name = "accounts/password_reset_done.html"


class PasswordResetConfirmView(auth_views.PasswordResetConfirmView):
    template_name = "accounts/password_reset_confirm.html"
    token_generator = password_reset_token_generator
    success_url = reverse_lazy("accounts:password_reset_complete")
    post_reset_login = False

    def get_user(self, uidb64):
        user = super().get_user(uidb64)
        return user if user is not None and user.status == User.Status.ACTIVE else None

    def form_valid(self, form):
        security_log.info("password_reset_completed user=%s", form.user.pk)
        return super().form_valid(form)


class PasswordResetCompleteView(auth_views.PasswordResetCompleteView):
    template_name = "accounts/password_reset_complete.html"


class PasswordChangeView(auth_views.PasswordChangeView):
    """Keeps the current session and ends all others (update_session_auth_hash)."""

    template_name = "accounts/password_change_form.html"
    success_url = reverse_lazy("accounts:password_change_done")

    def form_valid(self, form):
        security_log.info("password_changed user=%s", self.request.user.pk)
        return super().form_valid(form)


class PasswordChangeDoneView(auth_views.PasswordChangeDoneView):
    template_name = "accounts/password_change_done.html"


# --- Profile ------------------------------------------------------------------


class ProfileView(VerifiedAccountRequiredMixin, TemplateView):
    template_name = "accounts/profile.html"
