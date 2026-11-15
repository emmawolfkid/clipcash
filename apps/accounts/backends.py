from django.contrib.auth.backends import ModelBackend

from .phone_numbers import identifier_kind


class AccountStatusBackend(ModelBackend):
    """
    Authenticate only ACTIVE accounts.

    Django calls user_can_authenticate() both at login and when loading the user
    for every request (get_user), so suspending or disabling an account ends its
    existing sessions on their next request.

    REG-04: an email address signs in only when the account allows it
    (User.email_signs_in): members who signed up with a username use the username
    until the signed-in owner confirms the address. Usernames and mobile numbers are
    unaffected, and so is every account created before REG-04.
    """

    def authenticate(self, request, username=None, password=None, **kwargs):
        user = super().authenticate(request, username=username, password=password, **kwargs)
        if user is not None and identifier_kind(username) == "email" and not user.email_signs_in:
            return None  # Reported like any wrong sign-in: it does not say the address exists.
        return user

    def user_can_authenticate(self, user):
        return (
            super().user_can_authenticate(user)
            and getattr(user, "status", None) == user.Status.ACTIVE
        )
