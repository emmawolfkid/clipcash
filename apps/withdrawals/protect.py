"""
FIN-02: encryption of payout destination and identity numbers (Fernet: AES-128-CBC + HMAC-SHA256,
from the reviewed ``cryptography`` package).

Keys come only from the environment (DJANGO_PAYOUT_DATA_KEYS: comma-separated Fernet keys, newest
first; older keys keep decrypting during a rotation). Without a key nothing sensitive can be stored,
so real withdrawals refuse to start (check --deploy withdrawals.E001 when they are switched on).
Generate a key with:  python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
"""

from functools import lru_cache

from django.conf import settings


class ProtectionUnavailable(RuntimeError):
    """No payout data key is configured, or a stored value cannot be decrypted with it."""


@lru_cache(maxsize=4)
def _cipher(keys):
    from cryptography.fernet import Fernet, MultiFernet

    return MultiFernet([Fernet(key.encode()) for key in keys])


def _keys():
    return tuple(key.strip() for key in getattr(settings, "PAYOUT_DATA_KEYS", ()) if key and key.strip())


def available():
    return bool(_keys())


def encrypt(text):
    keys = _keys()
    if not keys:
        raise ProtectionUnavailable("No payout data key is configured (DJANGO_PAYOUT_DATA_KEYS).")
    return _cipher(keys).encrypt(str(text).encode()).decode()


def decrypt(token):
    from cryptography.fernet import InvalidToken

    keys = _keys()
    if not keys or not token:
        raise ProtectionUnavailable("The stored number cannot be read without the payout data key.")
    try:
        return _cipher(keys).decrypt(token.encode()).decode()
    except InvalidToken:
        raise ProtectionUnavailable("The stored number cannot be decrypted with the configured keys.") from None


def mask(text, visible=4):
    digits = "".join(ch for ch in str(text) if ch.isalnum())
    return "•" * max(0, len(digits) - visible) + digits[-visible:]
