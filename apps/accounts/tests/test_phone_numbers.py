from django.core.exceptions import ValidationError
from django.test import SimpleTestCase

from apps.accounts.phone_numbers import normalize_phone_number, validate_e164


class NormalizePhoneNumberTests(SimpleTestCase):
    def test_tanzanian_formats_normalize_to_e164(self):
        for raw in (
            "+255712345678",
            "+255 712 345 678",
            "00255712345678",
            "255712345678",
            "0712345678",
            "0712-345-678",
            "(0712) 345.678",
            " +255712345678 ",
        ):
            with self.subTest(raw=raw):
                self.assertEqual(normalize_phone_number(raw), "+255712345678")

    def test_empty_values_become_none(self):
        for raw in (None, "", "   ", " - "):
            with self.subTest(raw=raw):
                self.assertIsNone(normalize_phone_number(raw))

    def test_other_countries_accepted_in_international_format(self):
        self.assertEqual(normalize_phone_number("+1 415 555 2671"), "+14155552671")
        self.assertEqual(normalize_phone_number("0044 20 7946 0958"), "+442079460958")

    def test_invalid_numbers_rejected(self):
        for raw in (
            "712345678",  # ambiguous: no trunk prefix or calling code
            "071234567",  # too short for Tanzania
            "07123456789",  # too long for Tanzania
            "+2550712345678",  # trunk prefix kept after calling code
            "+25571234567",  # too short for Tanzania
            "+0712345678",  # calling code cannot start with 0
            "+255 71234567a",
            "+1234567",  # below E.164 minimum length
            "+1234567890123456",  # above E.164 maximum length
            "phone",
        ):
            with self.subTest(raw=raw), self.assertRaises(ValidationError):
                normalize_phone_number(raw)


class ValidateE164Tests(SimpleTestCase):
    def test_accepts_normalized_and_empty(self):
        validate_e164("+255712345678")
        validate_e164(None)
        validate_e164("")

    def test_rejects_unnormalized(self):
        for value in ("0712345678", "+255 712 345 678", "255712345678"):
            with self.subTest(value=value), self.assertRaises(ValidationError):
                validate_e164(value)
