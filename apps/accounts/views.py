import logging

from django.contrib import messages
from django.contrib.auth import get_user_model
from django.contrib.auth import views as auth_views
from django.contrib.auth.mixins import LoginRequiredMixin
from django.contrib.auth.views import INTERNAL_RESET_SESSION_TOKEN
from django.db import IntegrityError, transaction
from django.http import HttpResponseRedirect
from django.shortcuts import redirect, render
from django.urls import reverse_lazy
from django.utils import timezone
from django.utils.decorators import method_decorator
from django.utils.translation import gettext_lazy as _
from django.views import View
from django.views.decorators.debug import sensitive_post_parameters
from django.views.generic import FormView, TemplateView

from apps.audit.models import AuditEvent
from apps.audit.services import (
    DENIED,
    SUCCESS,
    hash_identifier,
    record_business_event,
    record_security_event,
)
from apps.core import features

from . import services
from .access import VerifiedAccountRequiredMixin
from .forms import (
    AccountPasswordChangeForm,
    AccountPasswordResetForm,
    EmailChangeForm,
    EmailRequestForm,
    LoginForm,
    ProfileForm,
    RegistrationForm,
)
from .models import PendingEmailChange
from .throttling import (
    EMAIL_CHANGE_REQUESTS_PER_ACCOUNT,
    EMAIL_CHANGE_REQUESTS_PER_ADDRESS,
    EMAIL_REQUESTS_PER_ACCOUNT,
    EMAIL_REQUESTS_PER_IP,
    REGISTRATIONS_PER_IP,
    client_ip_for_limits,
)
from .tokens import email_change_token_generator, password_reset_token_generator, verification_token_generator

security_log = logging.getLogger("clipcash.security")

User = get_user_model()

TOO_MANY_REQUESTS = _("Too many requests. Please wait a while and try again.")
INVALID_LINK_TEMPLATE = "accounts/link_invalid.html"


def _email_request_allowed(request, email):
    """Throttle outgoing account emails per IP (when the client IP is known) and per address."""
    ip = client_ip_for_limits(request)
    email = User.objects.normalize_email(email)
    if (ip and EMAIL_REQUESTS_PER_IP.is_limited(ip)) or EMAIL_REQUESTS_PER_ACCOUNT.is_limited(email):
        security_log.warning("email_request_throttled ip=%s", ip or "-")
        return False
    if ip:
        EMAIL_REQUESTS_PER_IP.hit(ip)
    EMAIL_REQUESTS_PER_ACCOUNT.hit(email)
    return True


def _email_change_allowed(request, new_email):
    """Limits per account, per requested address and per IP (when the client IP is known)."""
    ip = client_ip_for_limits(request)
    account = request.user.pk
    if (
        EMAIL_CHANGE_REQUESTS_PER_ACCOUNT.is_limited(account)
        or EMAIL_CHANGE_REQUESTS_PER_ADDRESS.is_limited(new_email)
        or (ip and EMAIL_REQUESTS_PER_IP.is_limited(ip))
    ):
        return False
    EMAIL_CHANGE_REQUESTS_PER_ACCOUNT.hit(account)
    EMAIL_CHANGE_REQUESTS_PER_ADDRESS.hit(new_email)
    if ip:
        EMAIL_REQUESTS_PER_IP.hit(ip)
    return True


# What the owner sees of their own account's audit trail: labels and times only,
# never addresses, IP details or metadata.
SECURITY_ACTIVITY = {
    "auth.login.succeeded": ("sign-in", _("Signed in")),
    "auth.login.failed": ("sign-in", _("Failed sign-in attempt")),
    "auth.password.changed": ("password", _("Password changed")),
    "auth.password_reset.completed": ("password", _("Password reset by email")),
    "auth.current_password.failed": ("password", _("Incorrect current password entered")),
    "account.email_change.requested": ("email", _("Email address change requested")),
    "account.email_change.cancelled": ("email", _("Email address change cancelled")),
    "account.email_changed": ("email", _("Email address changed")),
    "account.email_confirmed": ("email", _("Email address confirmed")),
    "account.profile_updated": ("profile", _("Profile updated")),
}


def _security_activity(user, limit=8):
    events = (AuditEvent.objects
              .filter(target_app_label="accounts", target_model="user", target_id=str(user.pk),
                      action__in=SECURITY_ACTIVITY)
              .order_by("-occurred_at").values("action", "occurred_at")[:limit])
    return [{"kind": SECURITY_ACTIVITY[event["action"]][0], "text": SECURITY_ACTIVITY[event["action"]][1],
             "when": event["occurred_at"]} for event in events]


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

    def dispatch(self, request, *args, **kwargs):
        # Checked on GET and POST; existing PENDING accounts can still activate.
        if not request.user.is_authenticated and not features.is_enabled("registration"):
            return render(request, "accounts/registration_closed.html", status=403)
        return super().dispatch(request, *args, **kwargs)

    def form_valid(self, form):
        ip = client_ip_for_limits(self.request)  # None: unknown client; the per-address limit still applies.
        if ip and REGISTRATIONS_PER_IP.is_limited(ip):
            security_log.warning("registration_throttled ip=%s", ip)
            form.add_error(None, TOO_MANY_REQUESTS)
            return self.form_invalid(form)
        if ip:
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
            record_security_event("auth.logout", outcome=SUCCESS, target=request.user)
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
        email = form.cleaned_data["email"]
        allowed = _email_request_allowed(self.request, email)
        record_security_event(
            "auth.password_reset.requested",
            outcome=SUCCESS if allowed else DENIED,
            metadata={"identifier_hash": hash_identifier(email), "throttled": not allowed},
        )
        if not allowed:
            return redirect(self.success_url)  # Same response; nothing is sent.
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
        with transaction.atomic():
            response = super().form_valid(form)  # Saves the new password.
            services.discard_email_change(form.user)
            record_business_event("auth.password_reset.completed", actor=form.user, target=form.user)
            services.notify_account(form.user, "password_reset", "Your password was reset",
                                    "If you did not do this, reset your password again now and contact ClipCash.",
                                    key=f"password_reset:{form.user.pk}:{timezone.now().isoformat()}")
        return response


class PasswordResetCompleteView(auth_views.PasswordResetCompleteView):
    template_name = "accounts/password_reset_complete.html"


class PasswordChangeView(auth_views.PasswordChangeView):
    """Keeps the current session and ends all others (update_session_auth_hash)."""

    template_name = "accounts/password_change_form.html"
    form_class = AccountPasswordChangeForm
    success_url = reverse_lazy("accounts:password_change_done")

    def form_valid(self, form):
        with transaction.atomic():
            response = super().form_valid(form)  # Saves the password, keeps this session.
            services.discard_email_change(self.request.user)
            record_business_event("auth.password.changed", target=self.request.user)
            services.notify_account(self.request.user, "password_changed", "Your password was changed",
                                    "Other sessions were signed out. If this was not you, reset your password now.",
                                    key=f"password_changed:{self.request.user.pk}:{timezone.now().isoformat()}")
        return response


class PasswordChangeDoneView(auth_views.PasswordChangeDoneView):
    template_name = "accounts/password_change_done.html"


# --- Profile ------------------------------------------------------------------


class ProfileView(VerifiedAccountRequiredMixin, TemplateView):
    template_name = "accounts/profile.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        membership = None
        from apps.core import features

        if features.is_enabled("memberships"):
            from apps.memberships.access import check_access

            membership = check_access(self.request.user).subscription
        context.update({
            "nav": "profile", "membership": membership, "breadcrumbs": [("Dashboard", "dashboard")],
            "pending_email_change": services.pending_email_change(self.request.user),
            "email_change_available": services.email_change_available(self.request.user),
            "activity": _security_activity(self.request.user),
        })
        return context


class AccountPageMixin(VerifiedAccountRequiredMixin):
    """A page inside "Profile and security". Every one acts on request.user alone."""

    success_url = reverse_lazy("accounts:profile")

    def get_context_data(self, **kwargs):
        return super().get_context_data(
            nav="profile", breadcrumbs=[("Dashboard", "dashboard"), ("Profile and security", "accounts:profile")],
            **kwargs,
        )


class ProfileEditView(AccountPageMixin, FormView):
    template_name = "accounts/profile_edit.html"
    form_class = ProfileForm

    def get_form_kwargs(self):
        # A separate copy: validating bad input must not alter request.user, which the page shows.
        return {**super().get_form_kwargs(), "instance": User.objects.get(pk=self.request.user.pk)}

    def form_valid(self, form):
        try:
            changed = services.update_profile(
                self.request.user, **{name: getattr(form.instance, name) for name in services.PROFILE_FIELDS}
            )
        except IntegrityError:  # Another account took the phone number after validation.
            form.add_error("phone_number", ProfileForm.Meta.error_messages["phone_number"]["unique"])
            return self.form_invalid(form)
        messages.success(self.request, _("Your profile was updated.") if changed else _("Nothing to change."))
        return super().form_valid(form)


class EmailChangeAvailableMixin:
    def dispatch(self, request, *args, **kwargs):
        if not services.email_change_available(request.user):
            messages.warning(request, _("Email address changes are not available for this account at the moment."))
            return redirect("accounts:profile")
        return super().dispatch(request, *args, **kwargs)


@method_decorator(sensitive_post_parameters("current_password"), name="dispatch")
class EmailChangeRequestView(AccountPageMixin, EmailChangeAvailableMixin, FormView):
    template_name = "accounts/email_change_form.html"
    form_class = EmailChangeForm

    def get_form_kwargs(self):
        return {**super().get_form_kwargs(), "user": self.request.user}

    def form_valid(self, form):
        new_email = form.cleaned_data["new_email"]
        if not _email_change_allowed(self.request, new_email):
            record_security_event("account.email_change.denied", outcome=DENIED, target=self.request.user,
                                  metadata={"reason": "throttled"})
            form.add_error(None, TOO_MANY_REQUESTS)
            return self.form_invalid(form)
        services.request_email_change(self.request.user, new_email, self.request)
        # Worded so it does not say whether the address is already registered.
        messages.success(self.request, _("If that address can be used, a confirmation link is on its way to it. "
                                         "Your email address stays the same until the link is used."))
        return super().form_valid(form)


class EmailChangeCancelView(VerifiedAccountRequiredMixin, View):
    http_method_names = ["post", "options"]

    def post(self, request):
        if services.cancel_email_change(request.user):
            messages.success(request, _("The email address change was cancelled."))
        return redirect("accounts:profile")


class EmailChangeConfirmView(View):
    """The emailed link. Works without signing in: the link proves the new inbox, and the
    password was checked when the change was requested. GET only shows a button, so link
    scanners cannot confirm; POST confirms once."""

    template_name = "accounts/email_change_confirm.html"

    def get(self, request, pk, token):
        change = PendingEmailChange.objects.select_related("user").filter(pk=pk).first()
        if change is None or not email_change_token_generator.check_token(change, token):
            return render(request, INVALID_LINK_TEMPLATE, status=400)
        return render(request, self.template_name, {"new_email": change.new_email})

    def post(self, request, pk, token):
        if services.confirm_email_change(pk, token) is None:
            return render(request, INVALID_LINK_TEMPLATE, status=400)
        return render(request, "accounts/email_change_complete.html")
