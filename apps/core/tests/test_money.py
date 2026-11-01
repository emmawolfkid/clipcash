from decimal import Decimal

from django.core.exceptions import ValidationError
from django.template import Context, Template
from django.test import SimpleTestCase

from apps.core.fields import AmountField
from apps.core.money import (
    MAX_AMOUNT,
    Money,
    MoneyError,
    format_amount,
    parse_amount,
    to_amount,
    validate_amount,
    validate_non_negative_amount,
)


class ToAmountTests(SimpleTestCase):
    def test_integers_accepted(self):
        for value in (0, 1, -5, 1500, MAX_AMOUNT, -MAX_AMOUNT):
            with self.subTest(value=value):
                self.assertEqual(to_amount(value), value)

    def test_integral_decimal_accepted(self):
        self.assertEqual(to_amount(Decimal("1500")), 1500)
        self.assertEqual(to_amount(Decimal("1500.00")), 1500)

    def test_floats_booleans_and_other_types_rejected(self):
        for value in (1.0, 0.1 + 0.2, float("nan"), True, False, "1500", None, [1]):
            with self.subTest(value=value), self.assertRaises(MoneyError):
                to_amount(value)

    def test_fractional_and_non_finite_decimals_rejected(self):
        for value in (Decimal("0.5"), Decimal("1500.01"), Decimal("NaN"), Decimal("Infinity")):
            with self.subTest(value=value), self.assertRaises(MoneyError):
                to_amount(value)

    def test_out_of_range_rejected(self):
        for value in (MAX_AMOUNT + 1, -MAX_AMOUNT - 1):
            with self.subTest(value=value), self.assertRaises(MoneyError):
                to_amount(value)


class MoneyTests(SimpleTestCase):
    def test_defaults_to_tzs(self):
        self.assertEqual(Money(100).currency, "TZS")

    def test_unsupported_currency_rejected(self):
        for currency in ("USD", "tzs", "", None):
            with self.subTest(currency=currency), self.assertRaises(MoneyError):
                Money(1, currency)

    def test_immutable(self):
        money = Money(100)
        with self.assertRaises(AttributeError):
            money.amount = 5

    def test_arithmetic(self):
        self.assertEqual(Money(1000) + Money(500), Money(1500))
        self.assertEqual(Money(1000) - Money(1500), Money(-500))
        self.assertEqual(-Money(5), Money(-5))
        self.assertEqual(Money(250) * 3, Money(750))
        self.assertEqual(3 * Money(250), Money(750))
        self.assertTrue(Money(-1).is_negative)
        self.assertFalse(Money.zero())

    def test_invalid_arithmetic_rejected(self):
        for operation in (
            lambda: Money(1) + 1,
            lambda: Money(1) * 1.5,
            lambda: Money(1) * True,
            lambda: Money(1) * Decimal("2"),
            lambda: Money(1) < 2,
        ):
            with self.subTest(operation=operation), self.assertRaises(MoneyError):
                operation()

    def test_result_overflow_rejected(self):
        with self.assertRaises(MoneyError):
            Money(MAX_AMOUNT) + Money(1)

    def test_comparisons(self):
        self.assertLess(Money(1), Money(2))
        self.assertGreaterEqual(Money(2), Money(2))
        self.assertEqual(sorted([Money(3), Money(1), Money(2)]), [Money(1), Money(2), Money(3)])

    def test_allocate_sums_exactly(self):
        for amount, ratios in ((100, [1, 1, 1]), (1001, [3, 7]), (7, [1, 1, 1, 1]), (0, [1, 2]),
                               (-100, [1, 1, 1]), (999_999, [5, 3, 2])):
            with self.subTest(amount=amount, ratios=ratios):
                parts = Money(amount).allocate(ratios)
                self.assertEqual(sum(p.amount for p in parts), amount)
                self.assertEqual(len(parts), len(ratios))

    def test_allocate_distribution(self):
        self.assertEqual([p.amount for p in Money(100).allocate([1, 1, 1])], [34, 33, 33])
        self.assertEqual([p.amount for p in Money(10).allocate([0, 1])], [0, 10])

    def test_allocate_invalid_ratios(self):
        for ratios in ([], [0, 0], [-1, 2], [1.5, 1]):
            with self.subTest(ratios=ratios), self.assertRaises(MoneyError):
                Money(10).allocate(ratios)


class ParseAndFormatTests(SimpleTestCase):
    def test_parse_valid(self):
        cases = {"1500": 1500, "1,500": 1500, " TZS 1,500 ": 1500, "TSh 25,000": 25000,
                 "0": 0, "-200": -200, "1,000,000": 1_000_000}
        for text, expected in cases.items():
            with self.subTest(text=text):
                self.assertEqual(parse_amount(text), expected)
        self.assertEqual(Money.parse("2,500"), Money(2500))

    def test_parse_invalid(self):
        for text in ("1500.50", "1.5", "15,00", "1,5000", "abc", "", "1e3", "USD 10", "--5", "1 500"):
            with self.subTest(text=text), self.assertRaises(MoneyError):
                parse_amount(text)
        with self.assertRaises(MoneyError):
            parse_amount(1500)

    def test_format(self):
        self.assertEqual(format_amount(1500), "TZS 1,500")
        self.assertEqual(format_amount(-25000), "-TZS 25,000")
        self.assertEqual(str(Money(1_000_000)), "TZS 1,000,000")
        with self.assertRaises(MoneyError):
            format_amount(1.5)

    def test_template_filter(self):
        rendered = Template("{% load money %}{{ a|tzs }}|{{ b|tzs }}").render(Context({"a": 1500, "b": 1.5}))
        self.assertEqual(rendered, "TZS 1,500|")


class AmountValidationTests(SimpleTestCase):
    def test_validators(self):
        validate_amount(-5)
        validate_non_negative_amount(0)
        for validator, value in ((validate_amount, 1.5), (validate_non_negative_amount, -1),
                                 (validate_amount, MAX_AMOUNT + 1)):
            with self.subTest(value=value), self.assertRaises(ValidationError):
                validator(value)

    def test_amount_field(self):
        field = AmountField(non_negative=True)
        field.clean(100, None)
        with self.assertRaises(ValidationError):
            field.clean(-1, None)
        _, path, _, kwargs = field.deconstruct()
        self.assertEqual(path, "apps.core.fields.AmountField")
        self.assertEqual(kwargs, {"non_negative": True})
        self.assertEqual(field.formfield().min_value, 0)
