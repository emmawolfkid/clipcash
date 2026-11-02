"""
Member wallet services (Phase 09). Wallets are identities over ledger accounts;
they hold NO money in this phase.

* Balances come only from JournalLine rows (``balances``); no balance column is
  ever trusted or edited.
* There is no credit, debit or transfer path. ``request_wallet_posting`` always
  refuses (and audits); the database independently refuses journal lines on
  wallet-linked accounts and any WalletTransaction (migration 0005).
* Demo rewards (apps.rewards) and sandbox payments (apps.payments) are never read
  as wallet money.
"""

from dataclasses import dataclass

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.db.models import Count, Q, Sum
from django.utils import timezone

from apps.audit.models import AuditEvent
from apps.audit.services import DENIED, SUCCESS, record_business_event, record_security_event

from . import services as ledger
from .chart import Scope
from .models import JournalLine, LedgerAccount, Wallet, WalletDiscrepancy, WalletRestriction, WalletTransaction

WALLET_ACCOUNT_TYPES = {"available": "REWARD_LIABILITY", "pending": "MEMBER_REWARD_PENDING",
                        "reserved": "MEMBER_WITHDRAWAL_RESERVED"}
MIN_REASON = 5
R = WalletRestriction.Status


class WalletError(Exception):
    code = "wallet_error"

    def __init__(self, message, *, code=None):
        super().__init__(message)
        if code:
            self.code = code


class NotEligible(WalletError):
    code = "not_eligible"


class NotAuthorized(WalletError):
    code = "not_authorized"


class InvalidState(WalletError):
    code = "invalid_state"


class WalletPostingLocked(WalletError):
    code = "wallet_postings_locked"


# --- Permissions and eligibility ---------------------------------------------------------


def _staff_with(user, perm):
    return bool(user is not None and getattr(user, "is_authenticated", False) and user.is_active
                and user.is_staff and user.has_perm(perm))


def can_view_wallets(user):
    return _staff_with(user, "wallets.view_wallets")


def can_manage_restrictions(user):
    return _staff_with(user, "wallets.manage_wallet_restrictions")


def _deny(action, actor, target=None, reason="permission_missing"):
    record_security_event("wallets.access.denied", outcome=DENIED,
                          actor=actor if getattr(actor, "is_authenticated", False) else None,
                          target=target, metadata={"action": action, "reason": reason})
    raise NotAuthorized("You are not allowed to do this.", code=reason)


def wallet_eligibility(user):
    """"ok" or why the account cannot hold a member wallet."""
    from apps.memberships.services import member_eligibility

    if user is None or not getattr(user, "is_authenticated", False):
        return "not_authenticated"
    if user.is_staff or user.role != get_user_model().Role.MEMBER:
        return "staff_account"  # Staff-only accounts never get member wallets.
    eligible, reason = member_eligibility(user)
    return "ok" if eligible else reason


# --- Wallet identity -----------------------------------------------------------------------


def wallet_for(user):
    if wallet_eligibility(user) != "ok":
        return None
    return Wallet.objects.filter(member=user).first()


def ensure_wallet(member):
    """Create the member's wallet (and its three empty ledger accounts) once.
    Idempotent and safe under concurrent requests."""
    reason = wallet_eligibility(member)
    if reason != "ok":
        raise NotEligible("This account cannot have a member wallet.", code=reason)
    existing = Wallet.objects.filter(member=member).first()
    if existing is not None:
        return existing
    try:
        with transaction.atomic():
            get_user_model().objects.select_for_update().get(pk=member.pk)  # Serialize per member.
            existing = Wallet.objects.filter(member=member).first()
            if existing is not None:
                return existing
            accounts = {bucket: ledger.open_account(account_type, scope_id=str(member.pk))
                        for bucket, account_type in WALLET_ACCOUNT_TYPES.items()}
            wallet = Wallet.objects.create(member=member, available_account=accounts["available"],
                                           pending_account=accounts["pending"], reserved_account=accounts["reserved"])
            record_business_event("wallets.wallet.created", actor=member, target=wallet,
                                  metadata={"number": wallet.number})
            return wallet
    except IntegrityError:
        return Wallet.objects.get(member=member)


def wallet_for_staff(actor, wallet_id, *, view="detail"):
    """Staff inspection (audited). Lookup by the wallet's random UUID only."""
    if not can_view_wallets(actor):
        _deny("wallet.inspect", actor)
    wallet = Wallet.objects.select_related("member").filter(pk=wallet_id).first()
    if wallet is None:
        raise NotEligible("Wallet not found.", code="not_found")
    record_security_event("wallets.wallet.inspected", outcome=SUCCESS, actor=actor, target=wallet,
                          metadata={"view": view})
    return wallet


# --- Balances (projection of the ledger) ---------------------------------------------------


@dataclass(frozen=True)
class WalletBalances:
    available: int
    pending: int
    reserved: int
    currency: str = "TZS"
    restricted: bool = False

    @property
    def spendable(self):
        """Only available funds of an unrestricted wallet; never pending or reserved."""
        return 0 if self.restricted else max(0, self.available)


def account_balances(account_ids):
    """{account_id: normal-side balance} for credit-normal liability accounts, computed
    from journal lines in one query (credits - debits). Exact integers."""
    totals = {account_id: 0 for account_id in account_ids}
    rows = (JournalLine.objects.filter(account_id__in=account_ids).values("account_id", "side")
            .annotate(total=Sum("amount")))
    for row in rows:
        sign = 1 if row["side"] == JournalLine.Side.CREDIT else -1
        totals[row["account_id"]] += sign * row["total"]
    return totals


def balances(wallet):
    ids = [wallet.available_account_id, wallet.pending_account_id, wallet.reserved_account_id]
    totals = account_balances(ids)
    return WalletBalances(available=totals[ids[0]], pending=totals[ids[1]], reserved=totals[ids[2]],
                          restricted=wallet.status == Wallet.Status.RESTRICTED)


def history(wallet):
    """Wallet transactions (always empty in Phase 09)."""
    return WalletTransaction.objects.filter(wallet=wallet).select_related("journal_entry").order_by("-created_at")


# --- The posting lock ----------------------------------------------------------------------


def request_wallet_posting(category, wallet, amount, *, actor=None, idempotency_key=""):
    """The future entry point for reward credits and withdrawal reservations.
    In Phase 09 it ALWAYS refuses: wallets hold no money. See docs/architecture/wallets.md
    for the contract a reviewed activation must meet."""
    record_security_event("wallets.posting.refused", outcome=DENIED,
                          actor=actor if getattr(actor, "is_authenticated", False) else None, target=wallet,
                          metadata={"category": str(category)[:30], "reason": "wallet_postings_locked"})
    raise WalletPostingLocked("Wallet money is not active. No credit, debit or transfer is possible.")


# --- Restrictions ----------------------------------------------------------------------------


def _reason(text, field="reason"):
    text = (text or "").strip()
    if len(text) < MIN_REASON:
        raise ValidationError({field: "Please give a reason (at least 5 characters)."})
    return text[:300]


def _sync_status(wallet):
    restricted = wallet.restrictions.exclude(status=R.LIFTED).exists()
    status = Wallet.Status.RESTRICTED if restricted else Wallet.Status.ACTIVE
    if wallet.status != status:
        wallet.status = status
        wallet.save(update_fields=["status", "updated_at"])


def place_restriction(actor, wallet, *, kind, reason):
    if not can_manage_restrictions(actor):
        _deny("restriction.place", actor, wallet)
    if kind not in WalletRestriction.Kind.values:
        raise ValidationError({"kind": "Choose a restriction type."})
    reason = _reason(reason)
    with transaction.atomic():
        wallet = Wallet.objects.select_for_update().get(pk=wallet.pk)
        restriction = WalletRestriction.objects.create(wallet=wallet, kind=kind, reason=reason, placed_by=actor)
        _sync_status(wallet)
        record_business_event("wallets.restriction.placed", actor=actor, target=wallet,
                              metadata={"kind": kind, "reason": reason[:100]})
    return restriction


def request_lift(actor, restriction, *, reason):
    if not can_manage_restrictions(actor):
        _deny("restriction.request_lift", actor, restriction.wallet)
    reason = _reason(reason)
    with transaction.atomic():
        restriction = WalletRestriction.objects.select_for_update().get(pk=restriction.pk)
        if restriction.status != R.ACTIVE:
            raise InvalidState("Only active restrictions can be lifted.")
        restriction.status = R.LIFT_REQUESTED
        restriction.lift_requested_by = actor
        restriction.lift_reason = reason
        restriction.save()
        record_business_event("wallets.restriction.lift_requested", actor=actor, target=restriction.wallet,
                              metadata={"kind": restriction.kind, "reason": reason[:100]})
    return restriction


def decide_lift(actor, restriction, *, approve, note=""):
    """Second person approves or declines the lift request (two-person rule)."""
    if not can_manage_restrictions(actor):
        _deny("restriction.decide_lift", actor, restriction.wallet)
    current = WalletRestriction.objects.get(pk=restriction.pk)  # Never trust a stale object.
    if current.lift_requested_by_id == actor.pk:
        _deny("restriction.decide_lift", actor, current.wallet, "four_eyes")
    with transaction.atomic():
        wallet = Wallet.objects.select_for_update().get(pk=restriction.wallet_id)
        restriction = WalletRestriction.objects.select_for_update().get(pk=restriction.pk)
        if restriction.status != R.LIFT_REQUESTED:
            return restriction
        if restriction.lift_requested_by_id == actor.pk:  # Re-checked on the locked row.
            raise NotAuthorized("The person who requested the lift cannot approve it.", code="four_eyes")
        if approve:
            restriction.status = R.LIFTED
            restriction.lifted_by = actor
            restriction.lifted_at = timezone.now()
        else:
            restriction.status = R.ACTIVE
            restriction.lift_requested_by = None
            restriction.lift_reason = ""
        restriction.save()
        _sync_status(wallet)
        record_business_event("wallets.restriction.lifted" if approve else "wallets.restriction.lift_declined",
                              actor=actor, target=wallet, metadata={"kind": restriction.kind,
                                                                    "note": (note or "")[:100]})
    return restriction


# --- Reconciliation ----------------------------------------------------------------------------


def _flag(findings, code, *, wallet=None, account=None, detail=""):
    findings.append(code)
    with transaction.atomic():
        exists = WalletDiscrepancy.objects.filter(wallet=wallet, account=account, code=code,
                                                  status=WalletDiscrepancy.Status.OPEN)
        if exists.exists():
            exists.update(last_seen_at=timezone.now())
            return
        WalletDiscrepancy.objects.create(wallet=wallet, account=account, code=code, detail=detail[:300])
        record_business_event("wallets.reconciliation.discrepancy", actor=None, target=wallet or account,
                              metadata={"code": code})


def reconcile(limit=1000):
    """Check every wallet against the ledger. Reports discrepancies; never edits history.
    Bounded and idempotent (an open finding is recorded once and refreshed)."""
    findings = []
    wallets = list(Wallet.objects.select_related("member", "available_account", "pending_account",
                                                 "reserved_account").order_by("created_at")[:limit])
    account_ids = [a for w in wallets for a in (w.available_account_id, w.pending_account_id, w.reserved_account_id)]
    line_totals = account_balances(account_ids)
    line_counts = dict(JournalLine.objects.filter(account_id__in=account_ids).values_list("account_id")
                       .annotate(n=Count("id")))
    created_events = set(AuditEvent.objects.filter(action="wallets.wallet.created", target_model="wallet")
                         .values_list("target_id", flat=True))
    restricted_ids = set(WalletRestriction.objects.exclude(status=R.LIFTED).values_list("wallet_id", flat=True))
    transaction_counts = dict(WalletTransaction.objects.values_list("wallet_id").annotate(n=Count("id")))
    for wallet in wallets:
        if wallet_eligibility(wallet.member) == "staff_account":
            _flag(findings, "member_not_eligible", wallet=wallet, detail="Wallet owner is a staff account.")
        for bucket, account_type in WALLET_ACCOUNT_TYPES.items():
            account = getattr(wallet, f"{bucket}_account")
            if (account.account_type != account_type or account.scope_type != Scope.MEMBER
                    or account.scope_id != str(wallet.member_id)):
                _flag(findings, "account_ownership", wallet=wallet, account=account,
                      detail=f"{bucket} account does not belong to this member.")
            projected = line_totals.get(account.pk, 0)
            if projected != account.balance:
                _flag(findings, "balance_projection_mismatch", wallet=wallet, account=account,
                      detail=f"Journal lines give {projected}; account balance is {account.balance}.")
            if projected != 0 or account.balance != 0 or line_counts.get(account.pk):
                _flag(findings, "unexpected_balance", wallet=wallet, account=account,
                      detail="Wallets must hold no money in this phase.")
        if transaction_counts.get(wallet.pk):
            _flag(findings, "unexpected_transactions", wallet=wallet, detail="Wallet transactions exist.")
        if str(wallet.pk) not in created_events:
            _flag(findings, "missing_audit", wallet=wallet, detail="No wallet-created audit event.")
        expected = Wallet.Status.RESTRICTED if wallet.pk in restricted_ids else Wallet.Status.ACTIVE
        if wallet.status != expected:
            _flag(findings, "status_mismatch", wallet=wallet, detail=f"Status {wallet.status}, expected {expected}.")
    # Wallet-type accounts of real members that are not linked to their wallet.
    member_ids = {str(pk) for pk in get_user_model().objects.values_list("pk", flat=True)}
    linked = set(account_ids)
    for account in LedgerAccount.objects.filter(account_type__in=WALLET_ACCOUNT_TYPES.values(),
                                                scope_type=Scope.MEMBER).exclude(pk__in=linked)[:limit]:
        if account.scope_id in member_ids:
            _flag(findings, "orphan_member_account", account=account,
                  detail="Member account not linked to the member's wallet.")
    return {"wallets": len(wallets), "findings": len(findings)}


def resolve_discrepancy(actor, discrepancy, *, note):
    if not can_manage_restrictions(actor):
        _deny("discrepancy.resolve", actor, discrepancy.wallet)
    note = _reason(note, "note")
    with transaction.atomic():
        discrepancy = WalletDiscrepancy.objects.select_for_update().get(pk=discrepancy.pk)
        if discrepancy.status == WalletDiscrepancy.Status.RESOLVED:
            return discrepancy
        discrepancy.status = WalletDiscrepancy.Status.RESOLVED
        discrepancy.resolved_by = actor
        discrepancy.resolved_at = timezone.now()
        discrepancy.resolution_note = note
        discrepancy.save()
        record_business_event("wallets.discrepancy.resolved", actor=actor, target=discrepancy.wallet,
                              metadata={"code": discrepancy.code, "note": note[:100]})
    return discrepancy


def directory(query=""):
    qs = Wallet.objects.select_related("member").order_by("-created_at")
    query = (query or "").strip()[:100]
    if query:
        qs = qs.filter(Q(number__iexact=query) | Q(member__email__iexact=query))
    return qs
