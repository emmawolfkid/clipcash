"""
The only supported way to write to the ledger.

post_entry() and reverse_entry() run in one transaction that:
 1. checks the posting type is registered and its financial feature enabled
    (all are locked off in this phase), and that the actor is authorized;
 2. validates every line (whole-shilling positive amounts, matching account
    types, active TZS accounts, no repeated account, debits == credits);
 3. returns the existing entry for a repeated idempotency key with the same
    content, and refuses a different posting under a used key;
 4. locks the affected accounts in a fixed order and refuses postings that
    would overdraw a protected account;
 5. inserts the entry and lines, then forces the database's deferred balance
    and funding-separation checks to run immediately;
 6. records the mandatory business audit event in the same transaction.

Any failure rolls the whole posting back: there are no partial entries.
Rejected attempts are recorded as security audit events outside the
rolled-back transaction.
"""

import hashlib
import json
from dataclasses import dataclass

from django.db import DatabaseError, IntegrityError, connection, transaction

from apps.audit import context as audit_context
from apps.audit.services import DENIED, FAILURE, record_business_event, record_security_event
from apps.core import features
from apps.core.money import MoneyError, to_amount

from .chart import ACCOUNT_TYPES, Scope, account_code
from .exceptions import (
    DuplicateReferenceError,
    IdempotencyConflictError,
    InsufficientFundsError,
    LedgerError,
    PostingDisabledError,
    PostingNotAuthorizedError,
    PostingValidationError,
    ReversalError,
)
from .models import JournalEntry, JournalLine, LedgerAccount
from .posting_types import POSTING_TYPES, SINGLE_USE_RELATED_TYPES

DEFERRED_CHECKS = "ledger_line_balanced, ledger_entry_complete"
MAX_SCOPE_ID = 64


@dataclass(frozen=True)
class Line:
    account: LedgerAccount
    side: str
    amount: int
    memo: str = ""


def debit(account, amount, memo=""):
    return Line(account, JournalLine.Side.DEBIT, amount, memo)


def credit(account, amount, memo=""):
    return Line(account, JournalLine.Side.CREDIT, amount, memo)


# --- Gates ----------------------------------------------------------------------


def _feature_enabled(key):
    """Single point the tests replace to exercise posting logic; in this phase
    every financial feature is locked off and this returns False."""
    return features.is_enabled(key)


def _authorize(actor):
    """System processes post with actor=None; people must be active Super Administrators."""
    if actor is None:
        return
    if not (getattr(actor, "is_authenticated", False) and actor.is_active and actor.is_superuser):
        raise PostingNotAuthorizedError("Only active Super Administrators may post ledger entries.")


# --- Accounts -------------------------------------------------------------------


def get_platform_account(account_type):
    return LedgerAccount.objects.get(account_type=account_type, scope_type=Scope.PLATFORM, scope_id="")


def open_account(account_type, *, scope_id, actor=None):
    """Open (or return) a scoped account, e.g. one campaign's restricted funding."""
    definition = ACCOUNT_TYPES.get(account_type)
    if definition is None:
        raise PostingValidationError(f"Unknown account type {account_type!r}.")
    if definition.scope == Scope.PLATFORM:
        raise PostingValidationError("Platform accounts are created by migration.")
    scope_id = str(scope_id or "").strip()
    if not scope_id or len(scope_id) > MAX_SCOPE_ID:
        raise PostingValidationError("A scope id of at most 64 characters is required.")
    _authorize(actor)

    with transaction.atomic():
        account, created = LedgerAccount.objects.get_or_create(
            account_type=account_type,
            scope_type=definition.scope,
            scope_id=scope_id,
            defaults={
                "code": account_code(account_type, scope_id),
                "name": definition.name,
                "classification": definition.classification,
                "funding_class": definition.funding_class,
                "overdraft_protected": definition.overdraft_protected,
            },
        )
        if created:
            record_business_event(
                "ledger.account.opened", actor=actor, target=account,
                metadata={"account_type": account_type, "scope_type": definition.scope},
            )
    return account


def set_account_status(account, status, *, actor):
    """ACTIVE <-> FROZEN; CLOSED (terminal) only with a zero balance."""
    if actor is None:
        raise PostingNotAuthorizedError("Account status changes need a person.")
    _authorize(actor)
    if status not in LedgerAccount.Status.values:
        raise PostingValidationError(f"Unknown account status {status!r}.")
    with transaction.atomic():
        account = LedgerAccount.objects.select_for_update().get(pk=account.pk)
        old = account.status
        if old == status:
            return account
        if old == LedgerAccount.Status.CLOSED:
            raise PostingValidationError("Closed accounts cannot be reopened.")
        if status == LedgerAccount.Status.CLOSED and account.debit_balance != 0:
            raise PostingValidationError("Only accounts with a zero balance can be closed.")
        account.status = status
        account.save(update_fields=["status", "updated_at"])
        record_business_event(
            "ledger.account.status_changed", actor=actor, target=account,
            metadata={"from_status": old, "to_status": status},
        )
    return account


# --- Posting --------------------------------------------------------------------


def _fingerprint(posting_type, lines, source_type, source_id, external_reference, related, reverses):
    payload = {
        "posting_type": posting_type,
        "lines": sorted((str(line.account.pk), line.side, line.amount) for line in lines),
        "source": [source_type, source_id, external_reference],
        "related": str(related.pk) if related else None,
        "reverses": str(reverses.pk) if reverses else None,
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


def _clean_lines(lines):
    if len(lines) < 2:
        raise PostingValidationError("A journal entry needs at least two lines.")
    cleaned = []
    for line in lines:
        if not isinstance(line, Line) or not isinstance(line.account, LedgerAccount):
            raise PostingValidationError("Lines must be built with debit() or credit().")
        if line.side not in JournalLine.Side.values:
            raise PostingValidationError("Invalid line side.")
        try:
            amount = to_amount(line.amount)
        except MoneyError as error:
            raise PostingValidationError(f"Invalid amount: {error}") from error
        if amount <= 0:
            raise PostingValidationError("Line amounts must be positive whole shillings.")
        cleaned.append(Line(line.account, line.side, amount, (line.memo or "")[:200]))
    if len({line.account.pk for line in cleaned}) != len(cleaned):
        raise PostingValidationError("An account may appear only once per entry.")
    debits = sum(line.amount for line in cleaned if line.side == JournalLine.Side.DEBIT)
    credits = sum(line.amount for line in cleaned if line.side == JournalLine.Side.CREDIT)
    if debits != credits:
        raise PostingValidationError(f"Entry is unbalanced: debits {debits}, credits {credits}.")
    return cleaned, debits


def _check_rules(posting, lines, total, actor, external_reference, related_entry, source_type):
    if external_reference and JournalEntry.objects.filter(
        source_type=source_type, external_reference=external_reference
    ).exists():
        raise DuplicateReferenceError("This external reference has already been posted.")
    if related_entry is not None and posting.code in SINGLE_USE_RELATED_TYPES and JournalEntry.objects.filter(
        posting_type=posting.code, related_entry=related_entry
    ).exists():
        raise DuplicateReferenceError("The related entry has already been used.")
    for line in lines:
        allowed = posting.debit_types if line.side == JournalLine.Side.DEBIT else posting.credit_types
        if line.account.account_type not in allowed:
            raise PostingValidationError(
                f"{posting.code} cannot {line.side.lower()} a {line.account.account_type} account."
            )
    if posting.requires_external_reference and not external_reference:
        raise PostingValidationError(f"{posting.code} requires an external reference.")
    if posting.same_scope and len({line.account.scope_id for line in lines}) != 1:
        raise PostingValidationError(f"{posting.code} lines must belong to the same owner.")
    if posting.related_type:
        if related_entry is None or related_entry.posting_type != posting.related_type:
            raise PostingValidationError(f"{posting.code} must reference a {posting.related_type} entry.")
        if JournalEntry.objects.filter(reverses=related_entry).exists():
            raise PostingValidationError("The related entry has been reversed.")
        if posting.related_amount_must_match and related_entry.total_amount != total:
            raise PostingValidationError("The amount must equal the related entry's amount.")
        if posting.distinct_actor:
            if actor is None:
                raise PostingNotAuthorizedError(f"{posting.code} must be performed by a person.")
            if related_entry.actor_id == actor.pk:
                raise PostingNotAuthorizedError("Verification must be performed by a different person.")
    elif related_entry is not None:
        raise PostingValidationError(f"{posting.code} does not take a related entry.")


def _lock_and_check_funds(lines):
    """Lock accounts in primary-key order (deadlock-free) and pre-check overdrafts.
    The database check constraint is the final authority."""
    ids = sorted({line.account.pk for line in lines})
    locked = {a.pk: a for a in LedgerAccount.objects.select_for_update().filter(pk__in=ids).order_by("pk")}
    for line in lines:
        account = locked[line.account.pk]
        if account.status != LedgerAccount.Status.ACTIVE:
            raise PostingValidationError(f"Account {account.code} is {account.status.lower()}.")
        if account.currency != "TZS":
            raise PostingValidationError("Only TZS accounts are supported.")
    for account in locked.values():
        delta = sum(
            (line.amount if line.side == JournalLine.Side.DEBIT else -line.amount)
            for line in lines if line.account.pk == account.pk
        )
        new_debit_balance = account.debit_balance + delta
        normal = new_debit_balance if account.classification in ("ASSET", "EXPENSE") else -new_debit_balance
        if account.overdraft_protected and normal < 0:
            raise InsufficientFundsError(f"Insufficient funds in {account.code}.")


def _translate_database_error(error):
    """Map ledger constraint violations to LedgerErrors; return None for anything
    else (e.g. a failed mandatory audit write), which must propagate unchanged."""
    message = str(error)
    if "ledger_account_no_overdraft" in message:
        return InsufficientFundsError("Insufficient funds (database check).")
    if "ledger_entry_external_reference_unique" in message:
        return DuplicateReferenceError("This external reference has already been posted.")
    if "ledger_entry_related_single_use" in message:
        return DuplicateReferenceError("The related entry has already been used.")
    if "reverses_id" in message:
        return ReversalError("This entry has already been reversed.")
    if "journal" in message or "ledger" in message:
        return PostingValidationError("The database rejected the posting.", code="database_rejected")
    return None


def _raise_translated(error):
    translated = _translate_database_error(error)
    if translated is None:
        raise error
    raise translated from error


def _insert(*, posting_type, lines, total, idempotency_key, fingerprint, description, source_type,
            source_id, external_reference, related_entry, reverses, actor):
    context = audit_context.current()
    entry = JournalEntry(
        posting_type=posting_type,
        description=description[:255],
        total_amount=total,
        source_type=source_type,
        source_id=str(source_id or "")[:64],
        external_reference=external_reference,
        related_entry=related_entry,
        reverses=reverses,
        idempotency_key=idempotency_key,
        fingerprint=fingerprint,
        actor_id=actor.pk if actor is not None else None,
        request_id=context.request_id if context else "",
    )
    entry.save(force_insert=True)
    JournalLine.objects.bulk_create(
        JournalLine(entry=entry, line_number=n, account=line.account, side=line.side,
                    amount=line.amount, memo=line.memo)
        for n, line in enumerate(lines, start=1)
    )
    with connection.cursor() as cursor:
        # Run the commit-time balance/separation checks now, inside this
        # savepoint, then defer them again for any later postings.
        cursor.execute(f"SET CONSTRAINTS {DEFERRED_CHECKS} IMMEDIATE")
        cursor.execute(f"SET CONSTRAINTS {DEFERRED_CHECKS} DEFERRED")
    return entry


def _existing(idempotency_key, fingerprint):
    entry = JournalEntry.objects.filter(idempotency_key=idempotency_key).first()
    if entry is not None and entry.fingerprint != fingerprint:
        raise IdempotencyConflictError("This idempotency key was used for a different posting.")
    return entry


def _record_rejection(posting_type, error, source_type, actor):
    record_security_event(
        "ledger.posting.rejected",
        outcome=DENIED if isinstance(error, (PostingDisabledError, PostingNotAuthorizedError)) else FAILURE,
        actor=actor,
        metadata={"posting_type": str(posting_type)[:64], "reason": error.code,
                  "source_type": str(source_type)[:40]},
    )


def post_entry(posting_type, *, lines, idempotency_key, source_type, description,
               source_id="", external_reference="", related_entry=None, actor=None):
    """Post one balanced entry. Returns the entry (the existing one on replay)."""
    try:
        posting = POSTING_TYPES.get(posting_type)
        if posting is None:
            raise PostingValidationError(f"Unknown posting type {posting_type!r}.")
        if not _feature_enabled(posting.required_feature):
            raise PostingDisabledError(f"{posting_type} is disabled ({posting.required_feature} is off).")
        _authorize(actor)
        if not idempotency_key or len(idempotency_key) > 128:
            raise PostingValidationError("An idempotency key of at most 128 characters is required.")
        if not source_type or len(source_type) > 40:
            raise PostingValidationError("A source type is required.")
        external_reference = (external_reference or "").strip()[:100]
        cleaned, total = _clean_lines(list(lines))
        fingerprint = _fingerprint(posting_type, cleaned, source_type, str(source_id or ""),
                                   external_reference, related_entry, None)

        existing = _existing(idempotency_key, fingerprint)
        if existing is not None:
            return existing

        try:
            with transaction.atomic():
                _check_rules(posting, cleaned, total, actor, external_reference, related_entry, source_type)
                _lock_and_check_funds(cleaned)
                entry = _insert(
                    posting_type=posting_type, lines=cleaned, total=total,
                    idempotency_key=idempotency_key, fingerprint=fingerprint,
                    description=description or posting.description, source_type=source_type,
                    source_id=source_id, external_reference=external_reference,
                    related_entry=related_entry, reverses=None, actor=actor,
                )
                record_business_event(
                    "ledger.entry.posted", actor=actor, target=entry,
                    metadata={"posting_type": posting_type, "total_amount": total,
                              "line_count": len(cleaned), "source_type": source_type},
                )
                return entry
        except IntegrityError as error:
            # A concurrent request may have stored the same idempotency key.
            existing = _existing(idempotency_key, fingerprint)
            if existing is not None:
                return existing
            _raise_translated(error)
        except DatabaseError as error:
            _raise_translated(error)
    except LedgerError as error:
        _record_rejection(posting_type, error, source_type, actor)
        raise


def reverse_entry(entry, *, idempotency_key, reason, actor):
    """Post the exact mirror of ``entry``. Allowed once, never for a reversal."""
    try:
        if actor is None:
            raise PostingNotAuthorizedError("Reversals must be authorized by a person.")
        _authorize(actor)
        posting = POSTING_TYPES.get(entry.posting_type)
        if posting is None or not _feature_enabled(posting.required_feature):
            raise PostingDisabledError(f"{entry.posting_type} reversals are disabled.")
        if not reason or not reason.strip():
            raise ReversalError("A reason is required.")
        if entry.reverses_id is not None:
            raise ReversalError("A reversal cannot itself be reversed.")
        if not idempotency_key or len(idempotency_key) > 128:
            raise PostingValidationError("An idempotency key of at most 128 characters is required.")

        original_lines = list(entry.lines.select_related("account").order_by("line_number"))
        mirrored = [
            Line(line.account,
                 JournalLine.Side.CREDIT if line.side == JournalLine.Side.DEBIT else JournalLine.Side.DEBIT,
                 line.amount, f"Reversal of line {line.line_number}")
            for line in original_lines
        ]
        fingerprint = _fingerprint(entry.posting_type, mirrored, "reversal", str(entry.pk), "", None, entry)
        existing = _existing(idempotency_key, fingerprint)
        if existing is not None:
            return existing

        try:
            with transaction.atomic():
                if JournalEntry.objects.filter(reverses=entry).exists():
                    raise ReversalError("This entry has already been reversed.")
                _lock_and_check_funds(mirrored)
                reversal = _insert(
                    posting_type=entry.posting_type, lines=mirrored, total=entry.total_amount,
                    idempotency_key=idempotency_key, fingerprint=fingerprint,
                    description=f"Reversal: {reason.strip()}"[:255], source_type="reversal",
                    source_id=str(entry.pk), external_reference="", related_entry=None,
                    reverses=entry, actor=actor,
                )
                record_business_event(
                    "ledger.entry.reversed", actor=actor, target=reversal,
                    metadata={"posting_type": entry.posting_type, "total_amount": entry.total_amount,
                              "original_entry": str(entry.pk)},
                )
                return reversal
        except IntegrityError as error:
            existing = _existing(idempotency_key, fingerprint)
            if existing is not None:
                return existing
            _raise_translated(error)
        except DatabaseError as error:
            _raise_translated(error)
    except LedgerError as error:
        _record_rejection(entry.posting_type, error, "reversal", actor)
        raise
