"""UI-03 real-form, token and presentation checks without database writes."""
import uuid
from datetime import datetime, timedelta
from unittest.mock import patch
from django.contrib.auth import get_user_model
from django.contrib.auth.models import AnonymousUser
from django.http import HttpResponse
from django.middleware.csrf import CsrfViewMiddleware
from django.template.loader import render_to_string
from django.test import RequestFactory, SimpleTestCase, override_settings
from django.utils import translation
from apps.accounts.forms import LoginForm, RegistrationForm, AccountPasswordResetForm, sign_in_key
from apps.accounts.tokens import AccountPasswordResetTokenGenerator
from apps.accounts import views
from .test_public_auth_ui import PublicAuthUITests, Elements


class AuthExperienceTests(SimpleTestCase):
    def context(self, path='/accounts/login/'):
        return PublicAuthUITests().context(path)

    def test_phone_first_label_keeps_the_single_alternative_identifier_field(self):
        with translation.override('en'):
            form = LoginForm()
            self.assertEqual(set(form.fields), {'username', 'password'})
            self.assertTrue(str(form.fields['username'].label).startswith('Mobile number'))
            self.assertEqual(form.fields['username'].widget.input_type, 'text')
            self.assertEqual(form.fields['username'].widget.attrs['autocomplete'], 'username')
            html = render_to_string('accounts/login.html', dict(self.context(), form=form))
            self.assertIn('name="username"', html)
            self.assertIn('name="password"', html)
            self.assertIn('button--action', html)
            self.assertIn('data-password-target="id_password"', html)
            self.assertIn('href="/accounts/password-reset/"', html)

    def test_existing_phone_email_and_username_normalization(self):
        for raw in ('0712345678', '+255712345678', '255712345678'):
            self.assertEqual(sign_in_key(raw), '+255712345678')
        self.assertEqual(sign_in_key('MEMBER@EXAMPLE.COM'), 'member@example.com')
        self.assertEqual(sign_in_key('Member_23'), 'member_23')

    def test_registration_accepts_each_existing_identifier_without_otp(self):
        for method, field, value in [('phone','phone_number','0712345678'),
                                     ('email','email','member@example.com'),
                                     ('username','username','member_23')]:
            with self.subTest(method=method):
                form = RegistrationForm({'first_name':'Amina','last_name':'Juma','contact_method':method,
                    field:value,'password1':'Sundial!Cobalt93','password2':'Sundial!Cobalt93'})
                self.assertTrue(form.is_valid(), form.errors)
                self.assertEqual(form.contact[0], method)
                self.assertNotIn('otp', form.fields)
                self.assertNotIn('role', form.fields)
        self.assertEqual(RegistrationForm().fields['contact_method'].initial, 'email')

    def test_registration_errors_and_values_remain_accessible(self):
        form = RegistrationForm({'first_name':'Amina','last_name':'Juma','contact_method':'phone',
            'phone_number':'bad','password1':'Sundial!Cobalt93','password2':'Different!Cobalt93'})
        self.assertFalse(form.is_valid())
        html = render_to_string('accounts/register.html', dict(self.context('/accounts/register/'), form=form))
        self.assertIn('href="#id_phone_number"', html)
        self.assertIn('href="#id_password2"', html)
        self.assertIn('aria-invalid="true"', html)
        self.assertIn('value="bad"', html)
        self.assertIn('data-password-target="id_password1"', html)
        self.assertIn('data-password-target="id_password2"', html)
        self.assertNotIn('value="Sundial!Cobalt93"', html)

    def test_recovery_email_validation_and_privacy_safe_done_copy(self):
        form = AccountPasswordResetForm({'email':'invalid'})
        self.assertFalse(form.is_valid())
        html = render_to_string('accounts/password_reset_form.html', dict(self.context(), form=form))
        self.assertIn('href="#id_email"', html)
        with translation.override('en'):
            done = render_to_string('accounts/password_reset_done.html', self.context())
            self.assertIn('If that address is confirmed on an active account', done)
            self.assertIn('works once', done)

    @override_settings(ACCOUNTS_PASSWORD_RESET_TIMEOUT=60, PASSWORD_RESET_TIMEOUT=3600)
    def test_password_tokens_expire_and_invalidate_after_password_change(self):
        user = get_user_model()(id=uuid.uuid4(), email='member@example.com', password='encoded-old',
                                status='ACTIVE', email_verified=True)
        token_generator = AccountPasswordResetTokenGenerator()
        now = datetime(2026, 1, 1, 12, 0)
        with patch.object(token_generator, '_now', return_value=now):
            token = token_generator.make_token(user)
            self.assertTrue(token_generator.check_token(user, token))
            self.assertFalse(token_generator.check_token(user, 'invalid'))
            user.password = 'encoded-new'
            self.assertFalse(token_generator.check_token(user, token))
            user.password = 'encoded-old'
        with patch.object(token_generator, '_now', return_value=now + timedelta(seconds=61)):
            self.assertFalse(token_generator.check_token(user, token))

    def test_auth_posts_without_csrf_are_rejected_before_any_view_runs(self):
        middleware = CsrfViewMiddleware(lambda request: HttpResponse('unused'))
        for route, view in [('/accounts/login/', views.LoginView), ('/accounts/register/', views.RegisterView),
                            ('/accounts/logout/', views.LogoutView), ('/accounts/password-reset/', views.PasswordResetView)]:
            with self.subTest(route=route):
                request = RequestFactory().post(route, {})
                request.user = AnonymousUser()
                # Exercise real CSRF rejection; isolate its failure renderer from site-configuration queries.
                with patch('django.middleware.csrf._get_failure_view', return_value=lambda request, reason: HttpResponse(status=403)):
                    rejected = middleware.process_view(request, view.as_view(), (), {})
                self.assertIsNotNone(rejected)
                self.assertEqual(rejected.status_code, 403)

    def test_brand_art_and_status_markers_have_no_duplicate_ids_or_earnings_claims(self):
        with translation.override('en'):
            html = render_to_string('accounts/register.html', dict(self.context(), form=RegistrationForm()))
            self.assertIn('class="clip-illustration" aria-hidden="true"', html)
            self.assertIn('Your account is ready at once', html)
            self.assertNotIn('Confirm your email address.', html)
            ids = [a['id'] for _, a in Elements(html).items if 'id' in a]
            self.assertEqual(len(ids), len(set(ids)))
            for name in ('link_invalid','registration_closed','password_reset_complete','email_change_confirm'):
                state = render_to_string('accounts/' + name + '.html', self.context())
                self.assertIn('class="auth-state auth-state--', state)
                self.assertIn('css/layouts/auth.css', state)
