"""
PRD-01: the boundary for proving ownership of a mobile number by SMS.

No SMS provider exists in this phase, so this module only fixes the contract that PRD-02
implements. Until then:

* sms_delivery_available() is False and nothing can mark a number verified
  (``User.phone_verified`` changes only through a future, provider-backed confirmation);
* there are no codes, no test codes and no development shortcut that could reach production;
* pages explain that phone verification and SMS recovery are not available yet, and members
  who want to buy a membership confirm an email address instead.

PRD-02 contract (not implemented here):

* request_phone_verification(user, request): send a single-use code, stored only as a keyed
  hash with a short expiry, a per-number/IP/account send limit and an attempt limit; audited
  without the code; never reveals whether another account uses the number.
* confirm_phone_verification(user, code): on success set phone_verified=True for the number
  the code was sent to (not whatever number the account holds now).
* request_phone_recovery(phone, request) / confirm_phone_recovery(...): only for a number
  that is already VERIFIED on an ACTIVE account; same generic response for unknown numbers;
  ends in the existing single-use password-reset form.
"""


class PhoneVerificationUnavailable(Exception):
    """SMS delivery is not configured: no code was or can be sent."""


def sms_delivery_available():
    """True only when a reviewed SMS provider is configured. Always False in PRD-01."""
    return False


def request_phone_verification(user, request=None):
    raise PhoneVerificationUnavailable("Phone verification by SMS is not available yet.")


def request_phone_recovery(phone_number, request=None):
    raise PhoneVerificationUnavailable("Password recovery by SMS is not available yet.")
