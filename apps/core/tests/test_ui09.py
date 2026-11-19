"""UI-09 language, branding, feedback and current financial wording contracts."""
import importlib.util

from django.conf import settings
from django.contrib.messages import constants
from django.contrib.messages.storage.base import Message
from django.template.loader import render_to_string
from django.test import SimpleTestCase, TestCase, override_settings
from django.urls import reverse
from django.utils import translation

from apps.accounts.forms import RegistrationForm
from apps.payments.models import ManualMembershipPayment
from apps.payments.tests.manual_helpers import ManualPaymentTestCase
from . import test_design_system


class BrandingAndFeedbackTests(SimpleTestCase):
    def test_shared_wordmark_in_header_auth_and_footer(self):
        context = test_design_system.DesignSystemTests().context('/accounts/login/')
        html = render_to_string('accounts/login.html', context)
        self.assertEqual(html.count('role="img" aria-label="ClipCash"'), 3)
        self.assertIn('wordmark--light', html)
        self.assertIn('images/favicon.svg', html)
        self.assertEqual(html.count('js/toasts.js'), 1)
        self.assertNotIn(' style=', html)

    def test_messages_deduplicate_escape_and_keep_all_severities(self):
        with translation.override('en'):
            html = render_to_string('components/messages.html', {'messages': [
                Message(constants.SUCCESS, 'Saved <script>'),
                Message(constants.SUCCESS, 'Saved <script>'),
                Message(constants.ERROR, 'Try again', extra_tags='custom'),
                Message(constants.WARNING, 'Review required'),
                Message(constants.INFO, 'Waiting'),
            ]})
        self.assertEqual(html.count('toast__text'), 4)
        self.assertEqual(html.count('Saved &lt;script&gt;'), 1)
        self.assertIn('messages__item--error toast', html)
        self.assertEqual(html.count('role="alert"'), 1)
        self.assertEqual(html.count('role="status"'), 3)
        self.assertEqual(html.count('aria-label="Dismiss notification" hidden'), 4)

    def test_success_can_be_explicitly_persistent(self):
        html = render_to_string('components/messages.html', {'messages': [
            Message(constants.SUCCESS, 'Next step needed'),
            Message(constants.SUCCESS, 'Next step needed', extra_tags='persistent'),
        ]})
        self.assertIn('data-persistent="true"', html)
        self.assertEqual(html.count('toast__text'), 1)

    def test_no_feedback_markup_when_no_messages(self):
        self.assertNotIn('toast-region', render_to_string('components/messages.html', {}))

    def test_catalogue_is_current_and_interpolation_safe(self):
        path = settings.BASE_DIR / 'scripts/verify_i18n.py'
        spec = importlib.util.spec_from_file_location('verify_i18n', path)
        verifier = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(verifier)
        self.assertEqual(verifier.MO.read_bytes(), verifier.compile_mo(verifier.catalogue()))

    def test_major_journey_template_strings_have_catalogue_entries(self):
        from scripts.verify_i18n import catalogue, inventory
        messages = {key.split('\0')[0] for key in catalogue()}
        areas = ('accounts', 'members', 'components', 'payments', 'memberships', 'rewards',
                 'viewing', 'videos', 'support', 'wallets', 'withdrawals', 'advertising', 'public', 'notifications')
        prefixes = tuple(f'templates/{area}/' for area in areas)
        missing = {key for key, paths in inventory().items()
                   if any(path.startswith(prefixes) for path in paths) and key not in messages}
        self.assertEqual(missing, set())

    def test_wordmark_colors_have_accessible_contrast(self):
        def luminance(color):
            values = [int(color[i:i + 2], 16) / 255 for i in (0, 2, 4)]
            linear = [value / 12.92 if value <= 0.04045 else ((value + 0.055) / 1.055) ** 2.4
                      for value in values]
            return sum(value * weight for value, weight in zip(linear, (0.2126, 0.7152, 0.0722)))
        navy = luminance('111b46')
        for foreground in ('ffffff', 'ffcf19'):
            self.assertGreaterEqual((luminance(foreground) + 0.05) / (navy + 0.05), 4.5)

    def test_credit_reason_translation_preserves_machine_codes(self):
        from apps.rewards.services import REASON_MESSAGES, reason_text
        codes = ['eligible', 'budget_exhausted']
        with translation.override('sw'):
            result = reason_text(codes)
        self.assertIn('Krediti zimetolewa', result)
        self.assertIn('Bajeti ya krediti ya kampeni iliisha.', result)
        self.assertEqual(codes, ['eligible', 'budget_exhausted'])
        self.assertEqual(REASON_MESSAGES['eligible'], 'Credited at the rate shown when you started watching.')

    def test_registration_and_validation_are_translated(self):
        with translation.override('sw'):
            form = RegistrationForm({'phone_number': 'invalid', 'password1': 'a', 'password2': 'b'})
            self.assertFalse(form.is_valid())
            self.assertEqual(str(form.fields['phone_number'].label), 'Namba ya simu')
            self.assertIn('Nenosiri na uthibitisho wake havifanani.', str(form.errors))

    def test_swahili_plural_forms_preserve_counts(self):
        with translation.override('sw'):
            for count in (1, 3):
                value = translation.ngettext('%(days)s day', '%(days)s days', count) % {'days': count}
                self.assertEqual(value, f'siku {count}')

    def test_credit_units_translate_without_changing_amounts_or_currency(self):
        from apps.core.templatetags.money import credits, tzs
        with translation.override('en'):
            self.assertEqual(credits(1), '1 credit')
            self.assertEqual(credits(1500), '1,500 credits')
        with translation.override('sw'):
            self.assertEqual(credits(1), 'krediti 1')
            self.assertEqual(credits(1500), 'krediti 1,500')
            self.assertEqual(tzs(1500), 'TZS 1,500')


@override_settings(STORAGES={'staticfiles': {'BACKEND': 'django.contrib.staticfiles.storage.StaticFilesStorage'}})
class LanguagePersistenceTests(TestCase):
    def setUp(self):
        self.addCleanup(translation.activate, translation.get_language())

    def test_selector_offers_both_languages_in_english(self):
        response = self.client.get(reverse('accounts:login'))
        self.assertContains(response, 'EN — English')
        self.assertContains(response, 'SW — Kiswahili')

    def test_language_cookie_survives_requests_and_can_switch_back(self):
        for code, text in (('sw', 'Ingia'), ('en', 'Sign in')):
            response = self.client.post(reverse('set_language'), {'language': code, 'next': reverse('accounts:login')})
            self.assertEqual(response.status_code, 302)
            self.assertEqual(response.cookies[settings.LANGUAGE_COOKIE_NAME].value, code)
            self.assertEqual(response.cookies[settings.LANGUAGE_COOKIE_NAME]['max-age'], 365 * 24 * 60 * 60)
            self.assertEqual(response.cookies[settings.LANGUAGE_COOKIE_NAME]['samesite'], 'Lax')
            self.assertEqual(bool(response.cookies[settings.LANGUAGE_COOKIE_NAME]['secure']), settings.SESSION_COOKIE_SECURE)
            for _ in range(2):
                page = self.client.get(response.url, HTTP_ACCEPT_LANGUAGE='en')
                self.assertContains(page, f'<html lang="{code}">')
                self.assertContains(page, f'<h1>{text}</h1>')

    def test_language_switch_rejects_external_destination(self):
        response = self.client.post(reverse('set_language'), {'language': 'sw', 'next': 'https://untrusted.example/'})
        self.assertEqual(response.url, '/')

    def test_language_switch_requires_csrf(self):
        from django.test import Client
        self.assertEqual(Client(enforce_csrf_checks=True).post(reverse('set_language'), {'language': 'sw'}).status_code, 403)


class SwahiliManualPaymentTests(ManualPaymentTestCase):
    def setUp(self):
        self.addCleanup(translation.activate, translation.get_language())
        super().setUp()
        self.client.force_login(self.member)
        self.client.cookies[settings.LANGUAGE_COOKIE_NAME] = 'sw'

    def test_payment_instructions_keep_real_money_and_verification_semantics(self):
        subscription = self.choose()
        response = self.client.get(reverse('payments:manual_pay', args=[subscription.pk]))
        self.assertContains(response, 'Lipia uanachama wako')
        self.assertContains(response, 'Airtel Money (Lipa)')
        self.assertContains(response, 'Kuwasilisha taarifa hizi pekee hakuwezeshi uanachama wako.')
        self.assertContains(response, 'Usimpe mtu yeyote PIN yako ya Airtel')
        self.assertNotContains(response, 'malipo ya kuigiza')

    def test_pending_and_approved_payments_have_distinct_translations(self):
        claim = self.submit()
        url = reverse('payments:manual_claim', args=[claim.pk])
        self.assertContains(self.client.get(url), 'Unasubiri uhakiki wa malipo')
        self.approve(claim)
        response = self.client.get(url)
        self.assertContains(response, 'Malipo yamethibitishwa: uanachama wako unatumika')
        claim.refresh_from_db()
        self.assertEqual(claim.status, ManualMembershipPayment.Status.APPROVED)

    def test_credit_disclaimer_never_promises_wallet_money(self):
        context = test_design_system.DesignSystemTests().context('/rewards/', authenticated=True)
        with translation.override('sw'):
            html = render_to_string('rewards/history.html', context)
        self.assertIn('Krediti si fedha', html)
        self.assertIn('haziwezi kutumika kulipia au kutolewa kama pesa', html)
