"""
Money conventions.

* Amounts are integers in the currency's smallest unit used by ClipCash. For
  Tanzanian shillings that is the whole shilling: ISO 4217 defines two decimal
  places for TZS, but senti are not used in practice and mobile-money providers
  settle in whole shillings. Fractional input is rejected, never rounded.
* Floats are never accepted. Decimal input is accepted only when it is integral.
* Arithmetic between different currencies is an error.
* Splitting uses the largest-remainder method so parts always sum to the whole.
"""

import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

from django.core.exceptions import ValidationError
from django.utils.translation import gettext_lazy as _

TZS = "TZS"
SUPPORTED_CURRENCIES = (TZS,)
DEFAULT_CURRENCY = TZS

# Upper bound for any single amount (one quadrillion shillings); far below the
# BigIntegerField limit so sums of many amounts cannot overflow PostgreSQL.
MAX_AMOUNT = 10**15

_AMOUNT_INPUT_RE = re.compile(r"^(?:TZS|TSH|TSh)?\s*(-)?\s*(\d{1,3}(?:,\d{3})+|\d+)$", re.IGNORECASE)


class MoneyError(ValueError):
    """Invalid money value or operation."""


def to_amount(value):
    """Convert ``value`` to an integer amount, rejecting floats and fractions."""
    if isinstance(value, bool) or isinstance(value, float):
        raise MoneyError("Money amounts must be integers; floats and booleans are not allowed.")
    if isinstance(value, int):
        amount = value
    elif isinstance(value, Decimal):
        if not value.is_finite() or value != value.to_integral_value():
            raise MoneyError("Money amounts must be whole shillings.")
        amount = int(value)
    else:
        raise MoneyError(f"Unsupported money amount type: {type(value).__name__}.")
    if abs(amount) > MAX_AMOUNT:
        raise MoneyError("Money amount is out of range.")
    return amount


def validate_currency(currency):
    if currency not in SUPPORTED_CURRENCIES:
        raise MoneyError(f"Unsupported currency: {currency!r}.")
    return currency


@dataclass(frozen=True, slots=True)
class Money:
    amount: int
    currency: str = DEFAULT_CURRENCY

    def __post_init__(self):
        object.__setattr__(self, "amount", to_amount(self.amount))
        validate_currency(self.currency)

    # Construction ---------------------------------------------------------

    @classmethod
    def zero(cls, currency=DEFAULT_CURRENCY):
        return cls(0, currency)

    @classmethod
    def parse(cls, text, currency=DEFAULT_CURRENCY):
        """Parse user input such as ``"1500"``, ``"1,500"`` or ``"TZS 1,500"``."""
        return cls(parse_amount(text), currency)

    # Arithmetic -------------------------------------------------------------

    def _check(self, other):
        if not isinstance(other, Money):
            raise MoneyError("Money can only be combined with Money.")
        if other.currency != self.currency:
            raise MoneyError("Cannot combine different currencies.")

    def __add__(self, other):
        self._check(other)
        return Money(self.amount + other.amount, self.currency)

    def __sub__(self, other):
        self._check(other)
        return Money(self.amount - other.amount, self.currency)

    def __neg__(self):
        return Money(-self.amount, self.currency)

    def __mul__(self, factor):
        if isinstance(factor, bool) or not isinstance(factor, int):
            raise MoneyError("Money can only be multiplied by an integer.")
        return Money(self.amount * factor, self.currency)

    __rmul__ = __mul__

    def __lt__(self, other):
        self._check(other)
        return self.amount < other.amount

    def __le__(self, other):
        self._check(other)
        return self.amount <= other.amount

    def __gt__(self, other):
        self._check(other)
        return self.amount > other.amount

    def __ge__(self, other):
        self._check(other)
        return self.amount >= other.amount

    def __bool__(self):
        return self.amount != 0

    @property
    def is_negative(self):
        return self.amount < 0

    def allocate(self, ratios):
        """
        Split into parts proportional to non-negative integer ``ratios``.
        Parts always sum exactly to this amount (largest remainder method).
        """
        ratios = [to_amount(r) for r in ratios]
        if not ratios or any(r < 0 for r in ratios) or sum(ratios) == 0:
            raise MoneyError("Ratios must be non-negative integers with a positive total.")
        if self.amount < 0:
            return [-part for part in (-self).allocate(ratios)]
        total = sum(ratios)
        parts = [self.amount * r // total for r in ratios]
        remainders = sorted(
            range(len(ratios)),
            key=lambda i: (self.amount * ratios[i] % total, -i),
            reverse=True,
        )
        for i in remainders[: self.amount - sum(parts)]:
            parts[i] += 1
        return [Money(p, self.currency) for p in parts]

    # Presentation -------------------------------------------------------------

    def __str__(self):
        return format_amount(self.amount, self.currency)


def parse_amount(text):
    """Parse whole-shilling user input; raise MoneyError on anything else."""
    if not isinstance(text, str):
        raise MoneyError("Amount input must be text.")
    match = _AMOUNT_INPUT_RE.match(text.strip())
    if not match:
        raise MoneyError("Enter a whole number of shillings, for example 1500 or 1,500.")
    sign, digits = match.groups()
    try:
        amount = int(Decimal(digits.replace(",", "")))
    except InvalidOperation as error:  # pragma: no cover - regex already guarantees digits
        raise MoneyError("Invalid amount.") from error
    return to_amount(-amount if sign else amount)


def format_amount(amount, currency=DEFAULT_CURRENCY):
    """``1500`` -> ``"TZS 1,500"``; negative amounts get a leading minus."""
    amount = to_amount(amount)
    sign = "-" if amount < 0 else ""
    return f"{sign}{validate_currency(currency)} {abs(amount):,}"


# Model-field validators -------------------------------------------------------


def validate_amount(value):
    try:
        to_amount(value)
    except MoneyError as error:
        raise ValidationError(_("Enter a valid amount in whole shillings."), code="invalid_amount") from error


def validate_non_negative_amount(value):
    validate_amount(value)
    if value < 0:
        raise ValidationError(_("The amount cannot be negative."), code="negative_amount")
