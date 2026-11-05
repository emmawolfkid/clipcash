"""
Readable text for internal codes shown on staff pages. Display only: stored values,
state transitions and audit event names never change. Known codes have written labels;
anything else is made readable from the code itself, so a new code is never hidden.
"""

from django.utils.translation import gettext_lazy as _

CODE_LABELS = {
    # Background jobs (apps.core.jobs.services.queue_health).
    "worker_heartbeat_stale": _("Background worker has not reported recently"),
    "queue_backlog": _("Job queue is backing up"),
    "expired_leases": _("Jobs held past their time limit"),
    "cache_unavailable": _("Shared cache is unavailable"),
    # Verified viewing flags (apps.administration.views.VIEWING_FLAGS).
    "excessive_events": _("Too many player events"),
    "heartbeat_gaps": _("Gaps between player updates"),
    "impossible_speed": _("Impossible playback speed"),
    "invalid_evidence": _("Invalid player evidence"),
    "large_seeks": _("Large jumps in the video"),
    # Reconciliation and exceptions.
    "unexpected_balance": _("Balance should be zero"),
    "payout_mismatch": _("Provider record does not match"),
    "payout_stuck": _("Payout has no final status"),
    "unknown_payout_reference": _("Unknown payout reference"),
}


def code_label(code):
    """``worker_heartbeat_stale`` -> a written label; ``wallets.access.denied`` -> ``Wallets: access denied``."""
    if code in (None, ""):
        return ""
    text = str(code)
    if text in CODE_LABELS:
        return CODE_LABELS[text]
    area, separator, rest = text.partition(".")
    if separator and rest:
        return f"{area.replace('_', ' ').capitalize()}: {rest.replace('.', ' ').replace('_', ' ')}"
    return text.replace("_", " ").capitalize()
