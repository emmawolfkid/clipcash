"""
REF-01: the referral programme's budget, funded ONLY by ClipCash's own marketing money.

1. ``record_deposit``: staff with referrals.record_marketing_deposits record a transfer of marketing
   money into ClipCash's payout float (Airtel Lipa business number or bank account): method, TZS
   amount, transaction reference, date. Nothing is posted: a typed reference is never proof.
2. ``verify_deposit``: a DIFFERENT person with referrals.verify_marketing_deposits confirms the money in
   the provider or bank records. Then, in one transaction, CASH -> marketing funds pending (with the
   channel reference, never reusable by a campaign deposit or a membership payment) -> verified.
3. ``commit_budget``: referrals.allocate_referral_budget commits verified marketing money to the
   referral programme account, the only source of referral rewards.
4. ``release_budget``: returns unused programme money to the verified marketing balance.

Membership fees, sandbox payments, simulated credits and advertiser money can never fund referral
rewards: the only inflow is a verified marketing deposit, and the ledger's database funding rules
refuse anything else. New money needs the referral_rewards feature; releasing needs only
referral_settlement.
"""

from datetime import timedelta

from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.db.models import Sum
from django.db.models.functions import Coalesce
from django.utils import timezone
from django.utils.translation import gettext as _
from django.utils.translation import gettext_noop

from apps.audit.services import record_business_event
from apps.earnings.funding import PAID_ON_MAX_AGE_DAYS, _amount, _membership_receipt_used, normalize_reference
from apps.wallets import services as ledger
from apps.wallets.exceptions import DuplicateReferenceError, InsufficientFundsError
from apps.wallets.models import JournalEntry, LedgerAccount

from .models import MarketingDeposit, ReferralFundingMovement, ReferralReward
from .services import (
    PROGRAMME_ACCOUNT,
    NotAuthorized,
    _notify_staff,
    _reason,
    _require,
    _staff_with,
    current_policy,
    peek_policy,
    rewards_active,
    settlement_active,
)

D = MarketingDeposit.Status
SOURCE_TYPE = "marketing_deposit"
PENDING_ACCOUNT = "MARKETING_FUNDING_PENDING"
VERIFIED_ACCOUNT = "MARKETING_FUNDING_VERIFIED"


def can_record(user):
    return _staff_with(user, "referrals.record_marketing_deposits")


def can_verify(user):
    return _staff_with(user, "referrals.verify_marketing_deposits")


def can_allocate(user):
    return _staff_with(user, "referrals.allocate_referral_budget")


def _lock_key(method, key):
    return f"{method}:{key}"


def _reference_used(method, key):
    """The same Airtel/bank transaction must never back two things (marketing deposit, campaign
    deposit or membership payment)."""
    from apps.earnings.models import CampaignDeposit

    lock = _lock_key(method, key)
    return (MarketingDeposit.objects.filter(reference_lock=lock).exists()
            or CampaignDeposit.objects.filter(reference_lock=lock).exists()
            or JournalEntry.objects.filter(source_type__in=[SOURCE_TYPE, "campaign_deposit"],
                                           external_reference=lock).exists()
            or _membership_receipt_used(method, key))


def record_deposit(actor, *, method, amount, external_reference, paid_on, note=""):
    _require(can_record, actor, "marketing_deposit.record")
    if method not in MarketingDeposit.Method.values:
        raise ValidationError({"method": _("Choose the payment method.")})
    amount = _amount(amount)
    text, key = normalize_reference(external_reference)
    today = timezone.localdate()
    if paid_on is None or paid_on > today or paid_on < today - timedelta(days=PAID_ON_MAX_AGE_DAYS):
        raise ValidationError({"paid_on": _("Enter the payment date (today or within the last 90 days).")})
    if _reference_used(method, key):
        raise ValidationError({"external_reference": _("This transaction reference has already been used.")})
    try:
        with transaction.atomic():
            deposit = MarketingDeposit.objects.create(
                method=method, amount=amount, external_reference=text, reference_key=key,
                reference_lock=_lock_key(method, key), paid_on=paid_on, note=(note or "").strip()[:300],
                recorded_by=actor)
            record_business_event("referrals.marketing_deposit.recorded", actor=actor, target=deposit, metadata={
                "reference": deposit.reference, "method": method, "amount": amount})
            _notify_staff("referrals.verify_marketing_deposits", actor, "referrals.deposit_verification_needed",
                          gettext_noop("A marketing budget deposit awaits verification"),
                          gettext_noop("Marketing deposit {reference} needs a second person to confirm the receipt."),
                          {"reference": deposit.reference}, inbox_link=("administration:referral_funding",),
                          key=str(deposit.pk))
    except IntegrityError:
        raise ValidationError({"external_reference": _("This transaction reference has already been used.")}) from None
    return deposit


def _require_rewards_active():
    if not rewards_active():
        raise ValidationError(_("Referral rewards are switched off for this deployment, so no money can be "
                                "posted. Deposits can be recorded now and verified once they are activated."))


def verify_deposit(actor, deposit, *, receipt_confirmed, expected_status=D.RECORDED):
    """Four-eyes verification of the actual receipt. Posts the deposit to the ledger exactly once."""
    _require(can_verify, actor, "marketing_deposit.verify")
    _require_rewards_active()
    if not receipt_confirmed:
        raise ValidationError({"receipt_confirmed": _(
            "Confirm that you found this payment in the Airtel Lipa business records or the bank statement.")})
    with transaction.atomic():
        deposit = MarketingDeposit.objects.select_for_update().get(pk=deposit.pk)
        if deposit.status == D.VERIFIED and expected_status == D.VERIFIED:
            return deposit
        if deposit.status != expected_status or deposit.status != D.RECORDED:
            raise ValidationError(_("This deposit was decided or changed since you opened it."))
        if deposit.recorded_by_id == actor.pk:
            raise NotAuthorized(_("A different person must verify the deposit you recorded."), code="four_eyes")
        pending = ledger.get_platform_account(PENDING_ACCOUNT)
        verified = ledger.get_platform_account(VERIFIED_ACCOUNT)
        try:
            received = ledger.post_entry(
                "marketing.deposit_received",
                lines=[ledger.debit(ledger.get_platform_account("CASH"), deposit.amount,
                                    f"{deposit.get_method_display()} receipt"),
                       ledger.credit(pending, deposit.amount, f"Marketing deposit {deposit.reference}")],
                idempotency_key=f"marketing-received:{deposit.pk}", source_type=SOURCE_TYPE,
                source_id=str(deposit.pk), external_reference=_lock_key(deposit.method, deposit.reference_key),
                description=f"Marketing deposit {deposit.reference}", actor=None)
        except DuplicateReferenceError:
            raise ValidationError({"external_reference": _("This transaction reference has already been used.")}) from None
        confirmed = ledger.post_entry(
            "marketing.deposit_verified",
            lines=[ledger.debit(pending, deposit.amount, "Verified by staff"),
                   ledger.credit(verified, deposit.amount, "Marketing budget available")],
            idempotency_key=f"marketing-verified:{deposit.pk}", source_type=SOURCE_TYPE, source_id=str(deposit.pk),
            related_entry=received, description=f"Marketing deposit {deposit.reference} verified", actor=None)
        deposit.status, deposit.decided_by, deposit.decided_at = D.VERIFIED, actor, timezone.now()
        deposit.received_entry, deposit.verified_entry = received, confirmed
        deposit.save(update_fields=["status", "decided_by", "decided_at", "received_entry", "verified_entry",
                                    "updated_at"])
        record_business_event("referrals.marketing_deposit.verified", actor=actor, target=deposit, metadata={
            "reference": deposit.reference, "method": deposit.method, "amount": deposit.amount})
    return deposit


def reject_deposit(actor, deposit, *, reason, expected_status=D.RECORDED):
    _require(can_verify, actor, "marketing_deposit.reject")
    reason = _reason(reason)
    with transaction.atomic():
        deposit = MarketingDeposit.objects.select_for_update().get(pk=deposit.pk)
        if deposit.status == D.REJECTED and expected_status == D.REJECTED:
            return deposit
        if deposit.status != expected_status or deposit.status != D.RECORDED:
            raise ValidationError(_("This deposit was decided or changed since you opened it."))
        if deposit.recorded_by_id == actor.pk:
            raise NotAuthorized(_("A different person must decide the deposit you recorded."), code="four_eyes")
        deposit.status, deposit.decided_by, deposit.decided_at = D.REJECTED, actor, timezone.now()
        deposit.decision_note, deposit.reference_lock = reason, None
        deposit.save(update_fields=["status", "decided_by", "decided_at", "decision_note", "reference_lock",
                                    "updated_at"])
        record_business_event("referrals.marketing_deposit.rejected", actor=actor, target=deposit, metadata={
            "reference": deposit.reference, "reason": reason[:100]})
    return deposit


def _balance(account):
    return -account.debit_balance if account is not None else 0


def _accounts_locked(*types):
    accounts = [ledger.get_platform_account(t) for t in types]
    locked = {a.pk: a for a in LedgerAccount.objects.select_for_update().filter(
        pk__in=sorted(a.pk for a in accounts)).order_by("pk")}
    return [locked[a.pk] for a in accounts]


def commit_budget(actor, *, amount, note=""):
    _require(can_allocate, actor, "budget.commit")
    _require_rewards_active()
    amount = _amount(amount)
    with transaction.atomic():
        source, target = _accounts_locked(VERIFIED_ACCOUNT, PROGRAMME_ACCOUNT)
        if _balance(source) < amount:
            raise ValidationError({"amount": _("There is not enough verified, unallocated marketing money.")})
        sequence = ReferralFundingMovement.objects.count() + 1
        try:
            entry = ledger.post_entry(
                "referral.funding_committed",
                lines=[ledger.debit(source, amount, "Committed to referral rewards"),
                       ledger.credit(target, amount, "Referral programme budget")],
                idempotency_key=f"referral-budget:{sequence}", source_type="referral_budget", source_id="programme",
                description="Marketing budget committed to referral rewards", actor=None)
        except InsufficientFundsError:
            raise ValidationError({"amount": _("There is not enough verified, unallocated marketing money.")}) from None
        movement = ReferralFundingMovement.objects.create(kind=ReferralFundingMovement.Kind.COMMIT, amount=amount,
                                                          entry=entry, actor=actor, reason=(note or "").strip()[:300])
        record_business_event("referrals.budget.committed", actor=actor, target=movement, metadata={"amount": amount})
    return movement


def release_budget(actor, *, amount, reason):
    """Return unused programme money to the verified marketing balance (never what rewards hold)."""
    _require(can_allocate, actor, "budget.release")
    if not settlement_active():
        raise ValidationError(_("Referral reward settlement is switched off, so no money can be posted."))
    amount = _amount(amount)
    reason = _reason(reason)
    with transaction.atomic():
        target, source = _accounts_locked(VERIFIED_ACCOUNT, PROGRAMME_ACCOUNT)
        free = _balance(source)
        if amount > free:
            raise ValidationError({"amount": _("Only %(free)s of the referral budget is unused and free to release.")
                                   % {"free": f"TZS {max(0, free):,}"}})
        sequence = ReferralFundingMovement.objects.count() + 1
        entry = ledger.post_entry(
            "referral.funding_released",
            lines=[ledger.debit(source, amount, "Unused referral budget released"),
                   ledger.credit(target, amount, "Returned to the marketing budget")],
            idempotency_key=f"referral-budget:{sequence}", source_type="referral_budget", source_id="programme",
            description="Unused referral budget released", actor=None)
        movement = ReferralFundingMovement.objects.create(kind=ReferralFundingMovement.Kind.RELEASE, amount=amount,
                                                          entry=entry, actor=actor, reason=reason)
        record_business_event("referrals.budget.released", actor=actor, target=movement, metadata={
            "amount": amount, "reason": reason[:100]})
    return movement


def budget_summary():
    """Ledger-derived referral budget figures (whole TZS)."""
    RW = ReferralReward.Status

    def total(qs):
        return qs.aggregate(t=Coalesce(Sum("amount"), 0))["t"]

    movements = ReferralFundingMovement.objects.all()
    rewards = ReferralReward.objects.all()
    accounts = {a.account_type: a for a in LedgerAccount.objects.filter(
        account_type__in=[PENDING_ACCOUNT, VERIFIED_ACCOUNT, PROGRAMME_ACCOUNT], scope_type="PLATFORM")}
    pending = total(rewards.filter(status=RW.PENDING))
    cleared = total(rewards.filter(status__in=[RW.AVAILABLE, RW.RECOVERY_REQUIRED]))
    balance = _balance(accounts.get(PROGRAMME_ACCOUNT))
    amount = peek_policy().reward_amount
    return {
        "verified": total(MarketingDeposit.objects.filter(status=D.VERIFIED)),
        "awaiting_verification": total(MarketingDeposit.objects.filter(status=D.RECORDED)),
        "unallocated": _balance(accounts.get(VERIFIED_ACCOUNT)),
        "committed": total(movements.filter(kind=ReferralFundingMovement.Kind.COMMIT)),
        "released": total(movements.filter(kind=ReferralFundingMovement.Kind.RELEASE)),
        "programme_balance": balance,  # The remaining referral budget.
        "pending": pending, "cleared": cleared,
        "reversed": total(rewards.filter(status=RW.REVERSED)),
        "allocated_net": pending + cleared,  # Rewards that have left the programme and not come back.
        "affordable_rewards": balance // amount if amount else None,
    }
