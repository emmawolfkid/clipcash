import logging
import re

# Account links carry single-use tokens in the path: activate/, confirm-email/
# and password reset/ (see apps/accounts/urls.py).
TOKEN_PATH = re.compile(r"/(activate|confirm-email|reset)/[^/\s]+/[^/\s]+")


class RedactTokenPaths(logging.Filter):
    """Removes account link tokens from log messages, e.g. Django's
    "Internal Server Error: /accounts/activate/<uid>/<token>/"."""

    def filter(self, record):
        message = record.getMessage()
        redacted = TOKEN_PATH.sub(r"/\1/[redacted]", message)
        if redacted != message:
            record.msg, record.args = redacted, None
        return True
