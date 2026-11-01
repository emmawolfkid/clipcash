from django.contrib.auth import SESSION_KEY, get_user_model, logout
from django.core.exceptions import ValidationError
from django.shortcuts import redirect
from django.urls import reverse


class AccountStatusMiddleware:
    """
    End sessions belonging to accounts that are no longer ACTIVE.

    The authentication backend already treats such sessions as anonymous; this
    middleware also flushes the session and explains why, instead of silently
    showing the user as signed out. It only queries when a session references a
    user the backend rejected.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if not request.user.is_authenticated and SESSION_KEY in request.session:
            User = get_user_model()
            try:
                user = User._default_manager.filter(pk=request.session[SESSION_KEY]).only("status").first()
            except (ValidationError, ValueError):
                user = None
            logout(request)
            if user is not None and user.status in (User.Status.SUSPENDED, User.Status.DISABLED):
                unavailable_url = reverse("accounts:account_unavailable")
                if request.path != unavailable_url:
                    return redirect(unavailable_url)
        return self.get_response(request)
