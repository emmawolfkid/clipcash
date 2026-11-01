from django.contrib.auth.backends import ModelBackend


class AccountStatusBackend(ModelBackend):
    """
    Authenticate only ACTIVE accounts.

    Django calls user_can_authenticate() both at login and when loading the user
    for every request (get_user), so suspending or disabling an account ends its
    existing sessions on their next request.
    """

    def user_can_authenticate(self, user):
        return (
            super().user_can_authenticate(user)
            and getattr(user, "status", None) == user.Status.ACTIVE
        )
