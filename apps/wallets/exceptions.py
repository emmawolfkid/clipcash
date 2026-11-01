class LedgerError(Exception):
    """Base class for posting failures. ``code`` is safe to log and audit."""

    code = "ledger_error"

    def __init__(self, message, *, code=None):
        super().__init__(message)
        if code:
            self.code = code


class PostingDisabledError(LedgerError):
    code = "feature_disabled"


class PostingNotAuthorizedError(LedgerError):
    code = "not_authorized"


class PostingValidationError(LedgerError):
    code = "invalid_posting"


class InsufficientFundsError(LedgerError):
    code = "insufficient_funds"


class IdempotencyConflictError(LedgerError):
    code = "idempotency_conflict"


class DuplicateReferenceError(LedgerError):
    code = "duplicate_reference"


class ReversalError(LedgerError):
    code = "invalid_reversal"
