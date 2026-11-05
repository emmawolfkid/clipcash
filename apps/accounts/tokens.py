"""
Single-use, expiring account tokens.

Both generators hash account state into the token, so a token stops working as
soon as that state changes (password set, email changed or verified, status
changed, or a login since issue). Django's own check enforces
PASSWORD_RESET_TIMEOUT as an outer limit; each generator applies its own,
shorter-or-equal lifetime on top.
"""

from django.conf import settings
from django.contrib.auth.tokens import PasswordResetTokenGenerator
from django.utils.http import base36_to_int


class _TimedAccountTokenGenerator(PasswordResetTokenGenerator):
    timeout_setting = None

    def _make_hash_value(self, user, timestamp):
        # Default includes pk, password hash, last_login, timestamp and email.
        return f"{super()._make_hash_value(user, timestamp)}{user.status}{user.email_verified}"

    def check_token(self, user, token):
        if not super().check_token(user, token):
            return False
        try:
            timestamp = base36_to_int(token.split("-")[0])
        except ValueError:
            return False
        age = self._num_seconds(self._now()) - timestamp
        return 0 <= age <= getattr(settings, self.timeout_setting)


class AccountVerificationTokenGenerator(_TimedAccountTokenGenerator):
    key_salt = "apps.accounts.tokens.AccountVerificationTokenGenerator"
    timeout_setting = "ACCOUNTS_EMAIL_VERIFICATION_TIMEOUT"


class AccountPasswordResetTokenGenerator(_TimedAccountTokenGenerator):
    key_salt = "apps.accounts.tokens.AccountPasswordResetTokenGenerator"
    timeout_setting = "ACCOUNTS_PASSWORD_RESET_TIMEOUT"


class EmailChangeTokenGenerator(_TimedAccountTokenGenerator):
    """
    Signs a PendingEmailChange, not a user. Bound to that request and the account's
    address, password and status. Unlike the others it ignores last_login, so the link
    still works when the owner has to sign in on another device to read the new inbox.
    Deleting the request row is what makes it single-use.
    """

    key_salt = "apps.accounts.tokens.EmailChangeTokenGenerator"
    timeout_setting = "ACCOUNTS_EMAIL_VERIFICATION_TIMEOUT"

    def _make_hash_value(self, change, timestamp):
        user = change.user
        return f"{change.pk}{change.new_email}{user.pk}{user.email}{user.password}{user.status}{timestamp}"


verification_token_generator = AccountVerificationTokenGenerator()
password_reset_token_generator = AccountPasswordResetTokenGenerator()
email_change_token_generator = EmailChangeTokenGenerator()
