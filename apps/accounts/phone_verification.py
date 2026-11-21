"""
Proving ownership of a mobile number with a one-time SMS code (PRD-01 contract, implemented
in PAY-LIVE-01F-B).

* request_phone_verification(user, request): sends a single-use 6-digit code to the account's
  CURRENT number. The code comes from ``secrets`` and is stored only as a keyed hash
  (HMAC-SHA256 with the site secret) bound to the challenge and to that exact number. It
  expires after PHONE_VERIFICATION_CODE_TTL_SECONDS. A new request supersedes the previous
  challenge and needs PHONE_VERIFICATION_RESEND_SECONDS to pass; sending is also limited per
  account, per number (whichever account asks) and per IP.
* confirm_phone_verification(user, code, request): checks the code against the account's
  active challenge, with the account and the challenge locked, so concurrent attempts are
  decided one at a time and a code works once. PHONE_VERIFICATION_MAX_ATTEMPTS wrong codes
  close the challenge. Success sets ``phone_verified`` only if the account still has the number
  the code was sent to; a changed number (which resets ``phone_verified``, see User.save)
  makes every earlier challenge useless.

Failures are generic (an invalid, expired or used code look the same) and never reveal
whether another account uses a number. Codes never appear in the database, logs, audit
events, messages or pages; only the SMS transport (apps.accounts.sms) receives one. With no
transport configured (the default) nothing can be sent or verified.

A verified number counts as a verified contact (User.has_verified_contact), which is what
starting a paid membership needs. It is NOT used to recover an account:
request_phone_recovery stays unavailable until a separate, reviewed recovery policy exists.
"""

import re
import secrets
from datetime import timedelta

from django.conf import settings
from django.contrib.auth import get_user_model
from django.db import transaction
from django.utils import timezone
from django.utils.crypto import constant_time_compare, salted_hmac

from apps.audit.services import DENIED, FAILURE, record_business_event, record_security_event

from . import sms, throttling

CODE_DIGITS = 6
CODE_RE = re.compile(r"^[0-9]{6}$")
HASH_SALT = "clipcash.accounts.phone-verification"
CLOSED_RETENTION = timedelta(days=1)


class PhoneVerificationError(Exception):
    code = "error"

    def __init__(self, message, *, code=None, retry_after=None):
        super().__init__(message)
        if code:
            self.code = code
        self.retry_after = retry_after


class PhoneVerificationUnavailable(PhoneVerificationError):
    """SMS delivery is not configured (or the message could not be sent): no code to use."""

    code = "unavailable"


class NothingToVerify(PhoneVerificationError):
    code = "nothing_to_verify"


class RateLimited(PhoneVerificationError):
    code = "rate_limited"


class InvalidCode(PhoneVerificationError):
    """Wrong, expired, already used or replaced: deliberately indistinguishable."""

    code = "invalid"


class AttemptsExhausted(PhoneVerificationError):
    code = "exhausted"


def sms_delivery_available():
    """True only when an SMS transport is configured (never in production yet)."""
    return sms.delivery_available()


def _challenge_model():
    from .models import PhoneVerificationChallenge

    return PhoneVerificationChallenge


def _code_hash(challenge_id, phone_number, code):
    return salted_hmac(HASH_SALT, f"{challenge_id}:{phone_number}:{code}", algorithm="sha256").hexdigest()


def _client_ip(request):
    if request is None:
        return ""
    address, _resolved = throttling.resolve_client_ip(request)
    return address or ""


def _lock_user(user):
    return get_user_model().objects.select_for_update().get(pk=user.pk)


def _refuse(action, user, reason, outcome=DENIED, **metadata):
    record_security_event(action, outcome=outcome, actor=user, target=user, metadata={"reason": reason, **metadata})


def active_challenge(user, now=None):
    """The account's open challenge for its current number, if it can still be used."""
    now = now or timezone.now()
    Challenge = _challenge_model()
    return (Challenge.objects.filter(user=user, status=Challenge.Status.ACTIVE, phone_number=user.phone_number,
                                     expires_at__gt=now)
            .order_by("-created_at").first())


def _message(code):
    minutes = max(1, settings.PHONE_VERIFICATION_CODE_TTL_SECONDS // 60)
    return (f"ClipCash code: {code}. It expires in {minutes} minutes. "
            "Never share it with anyone, including ClipCash staff.")


def request_phone_verification(user, request=None):
    """Send a code to the account's current number. Returns the challenge (never the code)."""
    if not sms_delivery_available():
        raise PhoneVerificationUnavailable("Phone verification by SMS is not available yet.")
    Challenge = _challenge_model()
    ip = _client_ip(request)
    now = timezone.now()
    refusal = None
    with transaction.atomic():
        user = _lock_user(user)  # One request at a time per account.
        phone = user.phone_number
        if not phone:
            refusal = NothingToVerify("Add a mobile number first.", code="no_number")
        elif user.phone_verified:
            refusal = NothingToVerify("This mobile number is already verified.", code="already_verified")
        else:
            latest = Challenge.objects.filter(user=user).order_by("-created_at").first()
            wait = settings.PHONE_VERIFICATION_RESEND_SECONDS
            if latest is not None and latest.created_at > now - timedelta(seconds=wait):
                retry = int((latest.created_at + timedelta(seconds=wait) - now).total_seconds()) + 1
                refusal = RateLimited("Please wait before asking for another code.", code="cooldown",
                                      retry_after=retry)
            elif (throttling.PHONE_CODES_PER_ACCOUNT.is_limited(user.pk)
                  or throttling.PHONE_CODES_PER_NUMBER.is_limited(phone)
                  or (ip and throttling.PHONE_CODES_PER_IP.is_limited(ip))):
                refusal = RateLimited("Too many codes were requested. Please try again later.", code="hourly_limit")
        if refusal is None:
            Challenge.objects.filter(user=user, status=Challenge.Status.ACTIVE).update(
                status=Challenge.Status.SUPERSEDED, closed_at=now)
            challenge = Challenge(user=user, phone_number=phone,
                                  expires_at=now + timedelta(seconds=settings.PHONE_VERIFICATION_CODE_TTL_SECONDS))
            code = f"{secrets.randbelow(10 ** CODE_DIGITS):0{CODE_DIGITS}d}"
            challenge.code_hash = _code_hash(challenge.pk, phone, code)
            challenge.save(force_insert=True)
            throttling.PHONE_CODES_PER_ACCOUNT.hit(user.pk)
            throttling.PHONE_CODES_PER_NUMBER.hit(phone)
            if ip:
                throttling.PHONE_CODES_PER_IP.hit(ip)
            record_business_event("account.phone_verification.requested", actor=user, target=user)
    if refusal is not None:
        _refuse("account.phone_verification.refused", user, refusal.code)
        raise refusal
    # Sent after the commit, with no lock held, so a slow provider never blocks other requests.
    try:
        sms.get_transport().send(phone, _message(code))
    except (sms.SmsUnavailable, sms.SmsDeliveryError):
        Challenge.objects.filter(pk=challenge.pk, status=Challenge.Status.ACTIVE).update(
            status=Challenge.Status.UNDELIVERED, closed_at=timezone.now())
        _refuse("account.phone_verification.refused", user, "undelivered", outcome=FAILURE)
        raise PhoneVerificationUnavailable("The code could not be sent. Please try again later.") from None
    finally:
        code = None  # noqa: F841 - drop the only reference to the code.
    return challenge


def confirm_phone_verification(user, code, request=None):
    """Check a code. Returns the account (phone_verified=True) or raises a generic error."""
    Challenge = _challenge_model()
    ip = _client_ip(request)
    if ip and throttling.PHONE_CODE_CHECKS_PER_IP.is_limited(ip):
        _refuse("account.phone_verification.failed", user, "ip_limit")
        raise RateLimited("Too many attempts. Please try again later.", code="ip_limit")
    if ip:
        throttling.PHONE_CODE_CHECKS_PER_IP.hit(ip)
    submitted = re.sub(r"\s", "", str(code or ""))
    submitted = submitted if CODE_RE.match(submitted) else None
    now = timezone.now()
    outcome, attempts = None, 0
    with transaction.atomic():
        user = _lock_user(user)  # Concurrent checks for one account are decided one at a time.
        challenge = (Challenge.objects.select_for_update().filter(user=user, status=Challenge.Status.ACTIVE)
                     .order_by("-created_at").first())
        if challenge is None:
            outcome = "no_challenge"
        elif challenge.phone_number != user.phone_number:
            challenge.status, challenge.closed_at = Challenge.Status.SUPERSEDED, now
            challenge.save(update_fields=["status", "closed_at", "updated_at"])
            outcome = "number_changed"
        elif challenge.expires_at <= now:
            challenge.status, challenge.closed_at = Challenge.Status.EXPIRED, now
            challenge.save(update_fields=["status", "closed_at", "updated_at"])
            outcome = "expired"
        else:
            challenge.attempts += 1
            attempts = challenge.attempts
            matches = submitted is not None and constant_time_compare(
                _code_hash(challenge.pk, challenge.phone_number, submitted), challenge.code_hash)
            if matches:
                challenge.status, challenge.closed_at = Challenge.Status.VERIFIED, now
                challenge.save(update_fields=["status", "closed_at", "attempts", "updated_at"])
                user.phone_verified = True
                user.save(update_fields=["phone_verified"])
                record_business_event("account.phone_verified", actor=user, target=user)
                outcome = "verified"
            elif attempts >= settings.PHONE_VERIFICATION_MAX_ATTEMPTS:
                challenge.status, challenge.closed_at = Challenge.Status.EXHAUSTED, now
                challenge.save(update_fields=["status", "closed_at", "attempts", "updated_at"])
                outcome = "exhausted"
            else:
                challenge.save(update_fields=["attempts", "updated_at"])
                outcome = "wrong_code"
    if outcome == "verified":
        return user
    # Recorded and raised after the commit, so the counted attempt is never rolled back.
    _refuse("account.phone_verification.failed", user, outcome, outcome=FAILURE, attempts=attempts)
    if outcome == "exhausted":
        raise AttemptsExhausted("Too many incorrect codes. Ask for a new code.")
    raise InvalidCode("That code is not valid. Check it, or ask for a new code.")


def prune_challenges(now=None):
    """Delete challenges closed (or expired) more than CLOSED_RETENTION ago. Returns the count."""
    Challenge = _challenge_model()
    cutoff = (now or timezone.now()) - CLOSED_RETENTION
    deleted, _ = Challenge.objects.exclude(status=Challenge.Status.ACTIVE).filter(closed_at__lt=cutoff).delete()
    stale, _ = Challenge.objects.filter(status=Challenge.Status.ACTIVE, expires_at__lt=cutoff).delete()
    return deleted + stale


def request_phone_recovery(phone_number, request=None):
    """Account recovery by SMS is NOT offered: it needs its own reviewed policy."""
    raise PhoneVerificationUnavailable("Password recovery by SMS is not available yet.")
