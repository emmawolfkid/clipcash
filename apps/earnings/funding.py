"""
FIN-01: manual campaign funding (advertiser organizations, ClipCash's own included).

1. ``record_deposit``: staff with earnings.record_deposits record a deposit (organization, method,
   TZS amount, transaction reference, date, payer, supporting information). Nothing is posted: an
   entered reference is never proof of payment.
2. ``verify_deposit``: a DIFFERENT staff member with earnings.verify_deposits confirms the money in
   the Airtel Lipa business records or the bank statement. Only then two ledger entries are posted
   in one transaction: CASH -> advertiser funds pending (with the channel reference, unique per
   channel), then pending -> verified. ``reject_deposit`` refuses it and frees the reference.
3. ``commit_funding``: staff with earnings.allocate_funding commit verified, unallocated funds of
   the organization to one of its campaigns (campaign.funding_committed). The first commitment
   marks the campaign VERIFIED, with that entry as database-checked evidence.
4. ``release_funding``: returns unused campaign funds (never what open offers have reserved) to the
   organization's verified balance (campaign.funding_released).

Membership fees, sandbox payments and simulated credits can never fund a campaign: the only
inflow is a verified deposit, and the ledger's funding rules refuse anything else.
"""

import re
from datetime import timedelta

from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.db.models import Sum
from django.db.models.functions import Coalesce
from django.utils import timezone
from django.utils.translation import gettext as _

from apps.advertising.models import Campaign, Organization
from apps.audit.services import record_business_event
from apps.core.money import MoneyError, to_amount
from apps.wallets import services as ledger
from apps.wallets.exceptions import DuplicateReferenceError, InsufficientFundsError
from apps.wallets.models import JournalEntry, LedgerAccount

from .models import CampaignDeposit, FundingMovement, SessionReward
from .services import NotAuthorized, _require, _staff_with, active, balance_of, funding_account, reserved_total

D = CampaignDeposit.Status
SOURCE_TYPE = "campaign_deposit"
FUNDABLE_STATES = (Campaign.Status.SCHEDULED, Campaign.Status.ACTIVE, Campaign.Status.PAUSED)
REFERENCE_RE = re.compile(r"^[A-Z0-9][A-Z0-9 .\-/]*[A-Z0-9]$")
MAX_DEPOSIT = 1_000_000_000
PAID_ON_MAX_AGE_DAYS = 90


def can_record(user):
    return _staff_with(user, "earnings.record_deposits")


def can_verify(user):
    return _staff_with(user, "earnings.verify_deposits")


def can_allocate(user):
    return _staff_with(user, "earnings.allocate_funding")


def can_view_funding(user):
    return can_record(user) or can_verify(user) or can_allocate(user) or _staff_with(user, "earnings.view_earnings")


def normalize_reference(raw):
    """(display text, key). Upper case, letters/digits and . - / ; the key keeps letters and digits."""
    text = " ".join(str(raw or "").split()).upper()
    if not text or len(text) > 40 or not REFERENCE_RE.match(text):
        raise ValidationError({"external_reference": _(
            "Enter the transaction reference exactly as it appears in the provider or bank record (letters and "
            "digits, up to 40 characters).")})
    key = re.sub(r"[^A-Z0-9]", "", text)
    if len(key) < 4:
        raise ValidationError({"external_reference": _("This transaction reference is too short.")})
    return text, key


def _channel_reference(method, key):
    return f"{method}:{key}"


def _membership_receipt_used(method, key):
    """The same Airtel/bank transaction must not also back a membership payment."""
    from apps.payments.manual import receipt_key
    from apps.payments.models import MANUAL_METHOD_AIRTEL_LIPA

    receipts = JournalEntry.objects.filter(source_type="manual_membership_payment")
    if method == CampaignDeposit.Method.AIRTEL_LIPA:
        return receipts.filter(external_reference=receipt_key(MANUAL_METHOD_AIRTEL_LIPA, key)).exists()
    # Bank channels are recorded as "<BANK method code>:<key>" (apps.payments.manual.receipt_key).
    return receipts.filter(external_reference__startswith="BANK", external_reference__endswith=f":{key}").exists()


def _amount(raw, field="amount"):
    if isinstance(raw, str):
        raw = raw.strip().replace(",", "")
        if not raw.isdigit():
            raise ValidationError({field: _("Enter a whole number of shillings.")})
        raw = int(raw)
    try:
        amount = to_amount(raw)
    except (MoneyError, ValueError, TypeError):
        raise ValidationError({field: _("Enter a whole number of shillings.")}) from None
    if amount <= 0 or amount > MAX_DEPOSIT:
        raise ValidationError({field: _("Enter an amount greater than zero.")})
    return amount


def record_deposit(actor, *, organization, method, amount, external_reference, paid_on, payer_name="", note=""):
    _require(can_record, actor, "deposit.record")
    if method not in CampaignDeposit.Method.values:
        raise ValidationError({"method": _("Choose the payment method.")})
    if organization is None or organization.verification_status != Organization.Verification.APPROVED:
        raise ValidationError({"organization": _("Choose an approved organization.")})
    amount = _amount(amount)
    text, key = normalize_reference(external_reference)
    today = timezone.localdate()
    if paid_on is None or paid_on > today or paid_on < today - timedelta(days=PAID_ON_MAX_AGE_DAYS):
        raise ValidationError({"paid_on": _("Enter the payment date (today or within the last 90 days).")})
    lock = _channel_reference(method, key)
    if (CampaignDeposit.objects.filter(reference_lock=lock).exists()
            or JournalEntry.objects.filter(source_type=SOURCE_TYPE, external_reference=lock).exists()
            or _membership_receipt_used(method, key)):
        raise ValidationError({"external_reference": _("This transaction reference has already been used.")})
    try:
        with transaction.atomic():
            deposit = CampaignDeposit.objects.create(
                organization=organization, method=method, amount=amount, external_reference=text,
                reference_key=key, reference_lock=lock, paid_on=paid_on, payer_name=(payer_name or "").strip()[:120],
                note=(note or "").strip()[:300], recorded_by=actor)
            record_business_event("earnings.deposit.recorded", actor=actor, target=deposit, metadata={
                "reference": deposit.reference, "organization": str(organization.pk), "method": method,
                "amount": amount})
            _notify_verifiers(deposit, actor)
    except IntegrityError:
        raise ValidationError({"external_reference": _("This transaction reference has already been used.")}) from None
    return deposit


def _require_active():
    if not active():
        raise ValidationError(_("Real TZS rewards are switched off for this deployment, so no money can be posted. "
                                "Deposits can be recorded and verified once they are activated."))


def _org_account(organization, account_type):
    return ledger.open_account(account_type, scope_id=str(organization.pk))


def verify_deposit(actor, deposit, *, receipt_confirmed, expected_status=D.RECORDED):
    """Four-eyes verification of the actual receipt. Posts the deposit to the ledger exactly once."""
    _require(can_verify, actor, "deposit.verify")
    _require_active()
    if not receipt_confirmed:
        raise ValidationError({"receipt_confirmed": _(
            "Confirm that you found this payment in the Airtel Lipa business records or the bank statement.")})
    with transaction.atomic():
        deposit = CampaignDeposit.objects.select_for_update().select_related("organization").get(pk=deposit.pk)
        if deposit.status == D.VERIFIED and expected_status == D.VERIFIED:
            return deposit
        if deposit.status != expected_status or deposit.status != D.RECORDED:
            raise ValidationError(_("This deposit was decided or changed since you opened it."))
        if deposit.recorded_by_id == actor.pk:
            raise NotAuthorized(_("A different person must verify the deposit you recorded."), code="four_eyes")
        pending = _org_account(deposit.organization, "ADVERTISER_FUNDING_PENDING")
        verified = _org_account(deposit.organization, "ADVERTISER_FUNDING_VERIFIED")
        lock = _channel_reference(deposit.method, deposit.reference_key)
        try:
            received = ledger.post_entry(
                "advertiser.manual_deposit_received",
                lines=[ledger.debit(ledger.get_platform_account("CASH"), deposit.amount,
                                    f"{deposit.get_method_display()} receipt"),
                       ledger.credit(pending, deposit.amount, f"Deposit {deposit.reference}")],
                idempotency_key=f"deposit-received:{deposit.pk}", source_type=SOURCE_TYPE,
                source_id=str(deposit.pk), external_reference=lock,
                description=f"Campaign deposit {deposit.reference} ({deposit.organization.display_name})"[:255],
                actor=None)
        except DuplicateReferenceError:
            raise ValidationError({"external_reference": _("This transaction reference has already been used.")}) from None
        confirmed = ledger.post_entry(
            "advertiser.manual_deposit_verified",
            lines=[ledger.debit(pending, deposit.amount, "Verified by staff"),
                   ledger.credit(verified, deposit.amount, "Available for campaigns")],
            idempotency_key=f"deposit-verified:{deposit.pk}", source_type=SOURCE_TYPE, source_id=str(deposit.pk),
            related_entry=received, description=f"Deposit {deposit.reference} verified"[:255], actor=None)
        deposit.status, deposit.decided_by, deposit.decided_at = D.VERIFIED, actor, timezone.now()
        deposit.received_entry, deposit.verified_entry = received, confirmed
        deposit.save(update_fields=["status", "decided_by", "decided_at", "received_entry", "verified_entry",
                                    "updated_at"])
        record_business_event("earnings.deposit.verified", actor=actor, target=deposit, metadata={
            "reference": deposit.reference, "organization": str(deposit.organization_id), "method": deposit.method,
            "amount": deposit.amount})
    return deposit


def reject_deposit(actor, deposit, *, reason, expected_status=D.RECORDED):
    _require(can_verify, actor, "deposit.reject")
    reason = (reason or "").strip()
    if len(reason) < 5:
        raise ValidationError({"reason": _("Please give a reason (at least 5 characters).")})
    with transaction.atomic():
        deposit = CampaignDeposit.objects.select_for_update().get(pk=deposit.pk)
        if deposit.status == D.REJECTED and expected_status == D.REJECTED:
            return deposit
        if deposit.status != expected_status or deposit.status != D.RECORDED:
            raise ValidationError(_("This deposit was decided or changed since you opened it."))
        if deposit.recorded_by_id == actor.pk:
            raise NotAuthorized(_("A different person must decide the deposit you recorded."), code="four_eyes")
        deposit.status, deposit.decided_by, deposit.decided_at = D.REJECTED, actor, timezone.now()
        deposit.decision_note, deposit.reference_lock = reason[:300], None
        deposit.save(update_fields=["status", "decided_by", "decided_at", "decision_note", "reference_lock",
                                    "updated_at"])
        record_business_event("earnings.deposit.rejected", actor=actor, target=deposit, metadata={
            "reference": deposit.reference, "organization": str(deposit.organization_id), "reason": reason[:100]})
    return deposit


def organization_balance(organization):
    """Verified, unallocated funds of the organization (TZS)."""
    account = LedgerAccount.objects.filter(account_type="ADVERTISER_FUNDING_VERIFIED",
                                           scope_id=str(organization.pk)).first()
    return balance_of(account)


def _lock_accounts(*accounts):
    ids = sorted(account.pk for account in accounts if account is not None)
    return {a.pk: a for a in LedgerAccount.objects.select_for_update().filter(pk__in=ids).order_by("pk")}


def commit_funding(actor, campaign, *, amount, note=""):
    _require(can_allocate, actor, "funding.commit")
    _require_active()
    amount = _amount(amount)
    with transaction.atomic():
        campaign = Campaign.objects.select_for_update().select_related("organization").get(pk=campaign.pk)
        if campaign.status not in FUNDABLE_STATES or campaign.approved_revision != campaign.revision:
            raise ValidationError({"campaign": _("Only approved, running or scheduled campaigns can be funded.")})
        organization = campaign.organization
        source = _org_account(organization, "ADVERTISER_FUNDING_VERIFIED")
        target = funding_account(campaign.pk, create=True)
        locked = _lock_accounts(source, target)
        if balance_of(locked[source.pk]) < amount:
            raise ValidationError({"amount": _("The organization does not have enough verified, unallocated funds.")})
        sequence = FundingMovement.objects.filter(campaign=campaign).count() + 1
        try:
            entry = ledger.post_entry(
                "campaign.funding_committed",
                lines=[ledger.debit(source, amount, "Committed to campaign"),
                       ledger.credit(target, amount, f"Funding for {campaign.name}"[:200])],
                idempotency_key=f"funding-commit:{campaign.pk}:{sequence}", source_type="campaign_funding",
                source_id=str(campaign.pk), description=f"Funding committed to {campaign.name}"[:255], actor=None)
        except InsufficientFundsError:
            raise ValidationError({"amount": _("The organization does not have enough verified, unallocated funds.")}) from None
        FundingMovement.objects.create(campaign=campaign, organization=organization, kind=FundingMovement.Kind.COMMIT,
                                       amount=amount, entry=entry, actor=actor, reason=(note or "").strip()[:300])
        if campaign.funding_status == Campaign.Funding.UNFUNDED:
            Campaign.objects.filter(pk=campaign.pk).update(funding_status=Campaign.Funding.VERIFIED,
                                                           funding_entry=entry, updated_at=timezone.now())
        record_business_event("earnings.funding.committed", actor=actor, target=campaign, metadata={
            "campaign": str(campaign.pk), "organization": str(organization.pk), "amount": amount})
    return entry


def release_funding(actor, campaign, *, amount, reason):
    """Return unused funds to the organization. Never touches what open offers have reserved."""
    _require(can_allocate, actor, "funding.release")
    _require_active()
    amount = _amount(amount)
    reason = (reason or "").strip()
    if len(reason) < 5:
        raise ValidationError({"reason": _("Please give a reason (at least 5 characters).")})
    with transaction.atomic():
        campaign = Campaign.objects.select_for_update().select_related("organization").get(pk=campaign.pk)
        source = funding_account(campaign.pk)
        if source is None:
            raise ValidationError({"amount": _("This campaign has no committed funding.")})
        target = _org_account(campaign.organization, "ADVERTISER_FUNDING_VERIFIED")
        locked = _lock_accounts(source, target)
        free = balance_of(locked[source.pk]) - reserved_total(campaign.pk)
        if amount > free:
            raise ValidationError({"amount": _("Only %(free)s of this campaign's funding is unused and free to "
                                               "release.") % {"free": f"TZS {max(0, free):,}"}})
        sequence = FundingMovement.objects.filter(campaign=campaign).count() + 1
        entry = ledger.post_entry(
            "campaign.funding_released",
            lines=[ledger.debit(source, amount, "Unused funding released"),
                   ledger.credit(target, amount, "Returned to the organization")],
            idempotency_key=f"funding-release:{campaign.pk}:{sequence}", source_type="campaign_funding",
            source_id=str(campaign.pk), description=f"Funding released from {campaign.name}"[:255], actor=None)
        FundingMovement.objects.create(campaign=campaign, organization=campaign.organization,
                                       kind=FundingMovement.Kind.RELEASE, amount=amount, entry=entry, actor=actor,
                                       reason=reason[:300])
        record_business_event("earnings.funding.released", actor=actor, target=campaign, metadata={
            "campaign": str(campaign.pk), "organization": str(campaign.organization_id), "amount": amount,
            "reason": reason[:100]})
    return entry


def campaign_summary(campaign, policy=None):
    """Ledger-derived funding figures for one campaign (whole TZS)."""
    movements = FundingMovement.objects.filter(campaign=campaign)
    committed = movements.filter(kind=FundingMovement.Kind.COMMIT).aggregate(t=Coalesce(Sum("amount"), 0))["t"]
    released = movements.filter(kind=FundingMovement.Kind.RELEASE).aggregate(t=Coalesce(Sum("amount"), 0))["t"]
    rewards = SessionReward.objects.filter(campaign=campaign)

    def total(**filters):
        return rewards.filter(**filters).aggregate(t=Coalesce(Sum("amount"), 0))["t"]

    balance = balance_of(funding_account(campaign.pk))
    reserved = total(status=SessionReward.Status.RESERVED)
    pending = total(status=SessionReward.Status.PENDING)
    available = total(status__in=[SessionReward.Status.AVAILABLE, SessionReward.Status.RECOVERY_REQUIRED])
    remaining = max(0, balance - reserved)
    amount = policy.reward_amount if policy is not None else 0
    return {
        "committed": committed, "released": released, "balance": balance, "reserved": reserved,
        "pending": pending, "available": available, "consumed": pending + available, "remaining": remaining,
        "affordable_views": remaining // amount if amount else None,
        "funded": campaign.funding_status == Campaign.Funding.VERIFIED,
    }


def _notify_verifiers(deposit, recorder):
    from django.utils.translation import gettext_noop

    from apps.notifications import inbox

    verifiers = [user for user in inbox.staff_with_permission("earnings.verify_deposits") if user.pk != recorder.pk]
    inbox.notify(verifiers, "earnings.deposit_verification_needed", category=inbox.Category.OPERATIONS,
                 title=gettext_noop("A campaign deposit awaits verification"),
                 message=gettext_noop("Deposit {reference} ({method__t}) needs a second person to confirm the "
                                      "receipt."),
                 params={"reference": deposit.reference,
                         "method__t": dict(METHOD_SOURCE).get(deposit.method, deposit.method)},
                 link=inbox.link_to("administration:campaign_deposit", deposit.pk),
                 dedupe_key=f"earnings.deposit_verification_needed:{deposit.pk}")


METHOD_SOURCE = (("AIRTEL_LIPA", "Airtel Lipa"), ("BANK_TRANSFER", "Bank transfer"))
