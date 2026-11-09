import logging
import re

# Account links carry single-use tokens in the path: activate/, confirm-email/,
# password reset/ and email-change/ (see apps/accounts/urls.py). RG-03 added
# email-change/, whose links were previously logged unredacted.
TOKEN_PATH = re.compile(r"/(activate|confirm-email|reset|email-change)/[^/\s]+/[^/\s]+")


class RedactTokenPaths(logging.Filter):
    """Removes account link tokens from log messages, e.g. Django's
    "Internal Server Error: /accounts/activate/<uid>/<token>/"."""

    def filter(self, record):
        message = record.getMessage()
        redacted = TOKEN_PATH.sub(r"/\1/[redacted]", message)
        if redacted != message:
            record.msg, record.args = redacted, None
        return True
