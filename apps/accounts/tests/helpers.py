import re

from django.contrib.auth import get_user_model
from django.core import mail
from django.core.cache import cache
from django.test import TestCase

User = get_user_model()

PASSWORD = "Correct-Horse-Battery-9"
NEW_PASSWORD = "Another-Strong-Phrase-42"


def make_user(
    email="member@example.com",
    *,
    role=User.Role.MEMBER,
    status=User.Status.ACTIVE,
    verified=True,
    password=PASSWORD,
):
    if role == User.Role.SUPER_ADMIN:
        user = User.objects.create_superuser(email, password)
    else:
        user = User.objects.create_user(email, password, role=role, status=status)
    if user.status != status or user.email_verified != verified:
        user.status = status
        user.email_verified = verified
        user.save()
    return user


def links_in_last_email():
    return re.findall(r"https?://\S+", mail.outbox[-1].body)


class AccountsTestCase(TestCase):
    """Clears the rate-limit cache so tests are independent."""

    def setUp(self):
        super().setUp()
        cache.clear()
