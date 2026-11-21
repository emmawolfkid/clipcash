"""
PRD-04: receipt evidence for manual membership payment claims.

Evidence is the member's own screenshot or PDF of the provider's confirmation. It helps a reviewer
find the transaction; it is never proof of payment (apps.payments.manual.approve still needs the
transaction found in the official provider records).

* Type is decided by the file's CONTENT (JPEG, PNG or PDF signatures), never by its name or the
  browser's declared type; anything else is refused. Size is limited
  (settings.PAYMENTS_EVIDENCE_MAX_BYTES).
* Files are stored in private storage (settings.PAYMENTS_EVIDENCE_ROOT), never under STATIC_ROOT
  or MEDIA_ROOT, under a random name in a folder per claim, owner-only permissions. The original
  file name is not kept (it can contain personal data and is not needed).
* Files are read back only through payments:manual_evidence, for the claim's member and staff who
  may view manual payments, as a download (never rendered inline by the site).
"""

import hashlib
import uuid

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.files.storage import FileSystemStorage
from django.utils.translation import gettext_lazy as _

from .models import EVIDENCE_TYPES

SIGNATURES = (
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"%PDF-", "application/pdf"),
)


def storage():
    return FileSystemStorage(location=str(settings.PAYMENTS_EVIDENCE_ROOT), base_url=None,
                             file_permissions_mode=0o600, directory_permissions_mode=0o700)


def inspect(upload, field="evidence"):
    """Validate an uploaded file. Returns (content_type, size, sha256) or raises ValidationError."""
    limit = settings.PAYMENTS_EVIDENCE_MAX_BYTES
    size = getattr(upload, "size", 0) or 0
    if size <= 0:
        raise ValidationError({field: _("The file is empty.")}, code="empty")
    if size > limit:
        raise ValidationError({field: _("The file is too large. The limit is %(mb)s MB.") % {"mb": limit // (1024 * 1024)}},
                              code="too_large")
    upload.seek(0)
    head = upload.read(16)
    upload.seek(0)
    content_type = next((kind for signature, kind in SIGNATURES if head.startswith(signature)), None)
    if content_type is None:
        raise ValidationError({field: _("Upload a JPEG or PNG picture, or a PDF, of the payment confirmation.")},
                              code="type")
    digest = hashlib.sha256()
    for chunk in upload.chunks():
        digest.update(chunk)
    upload.seek(0)
    return content_type, size, digest.hexdigest()


def save(claim, upload, content_type):
    """Store the file privately; returns the stored name (relative to the evidence root)."""
    name = f"{claim.pk.hex}/{uuid.uuid4().hex}.{EVIDENCE_TYPES[content_type]}"
    stored = storage().save(name, upload).replace("\\", "/")
    if stored != name:  # A random name cannot collide; anything else is a storage problem.
        delete_quietly(stored)
        raise OSError("evidence storage changed the file name")
    return stored


def delete_quietly(name):
    """Remove a stored file whose database row was never committed (best effort)."""
    try:
        storage().delete(name)
    except OSError:
        pass


def open_file(evidence):
    return storage().open(evidence.storage_name, "rb")
