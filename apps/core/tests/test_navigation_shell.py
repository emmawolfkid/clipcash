"""UI-02 shell contracts; no application database is needed."""
from django.template.loader import render_to_string
from django.test import SimpleTestCase
from django.utils import translation
from .test_design_system import DesignSystemTests
from .test_public_auth_ui import Elements


class NavigationShellTests(SimpleTestCase):
    def context(self, path='/dashboard/', **extra):
        context = DesignSystemTests().context(path, authenticated=True)
        context.update(csrf_token='test-token', nav='dashboard')
        context.update(extra)
        return context

    def test_member_active_link_icons_and_supported_routes(self):
        cases = [('dashboard', '/dashboard/'), ('activity', '/dashboard/activity/'),
                 ('videos', '/videos/'), ('watch', '/watch/'), ('rewards', '/rewards/'),
                 ('membership', '/memberships/mine/'), ('payments', '/payments/'),
                 ('wallet', '/wallet/'), ('withdrawals', '/withdrawals/'),
                 ('advertiser', '/advertisers/'), ('notifications', '/notifications/'),
                 ('support', '/support/'), ('profile', '/accounts/')]
        for nav, target in cases:
            with self.subTest(nav=nav):
                html = render_to_string('components/member_sidebar.html', self.context(nav=nav, sandbox_payments=True))
                current = [a for tag, a in Elements(html).items if tag == 'a' and a.get('aria-current') == 'page']
                self.assertEqual([a['href'] for a in current], [target])
                self.assertNotIn('/referral', html)
                self.assertEqual(html.count('class="nav-icon"'), 14)
                self.assertIn('data-nav-close', html)

    def test_disabled_features_remove_destinations(self):
        context = self.context(features={}, sandbox_payments=False)
        html = render_to_string('components/member_sidebar.html', context)
        for route in ('/videos/', '/watch/', '/rewards/', '/memberships/mine/', '/payments/', '/advertisers/'):
            self.assertNotIn('href="' + route + '"', html)
        self.assertIn('href="/wallet/"', html)

    def test_console_capabilities_keep_every_existing_destination(self):
        capabilities = ['members','plans','membership_payments','payment_instructions','support',
                        'advertisers','campaigns','videos','categories','viewing','rewards','payments',
                        'wallets','withdrawals','reports','analytics','settings','audit']
        context = self.context('/manage/', can=dict.fromkeys(capabilities, True), nav='payments')
        html = render_to_string('administration/layout.html', context)
        for route in ['members','plans','membership-payments','membership-payments/instructions','support',
                      'advertisers','campaigns','videos','categories','viewing','rewards','payments',
                      'wallets','withdrawals','reports','analytics','security','health']:
            self.assertIn('href="/manage/' + route + '/"', html)
        for route in ('/admin/accounts/user/', '/admin/core/siteconfiguration/', '/admin/audit/auditevent/'):
            self.assertIn('href="' + route + '"', html)
        hidden = render_to_string('administration/layout.html', self.context('/manage/', can={}))
        for route in ('members','plans','membership-payments','support','advertisers','campaigns','videos',
                      'categories','viewing','rewards','payments','wallets','withdrawals','reports','analytics','security'):
            self.assertNotIn('href="/manage/' + route + '/"', hidden)

    def test_header_counts_profile_and_language_ids_across_shells(self):
        for template, route in [('members/layout.html','/dashboard/'), ('advertising/layout.html','/advertisers/'),
                                ('administration/layout.html','/manage/'), ('public/home.html','/')]:
            for count in (0, 3, 105):
                with self.subTest(template=template, count=count), translation.override('sw'):
                    html = render_to_string(template, self.context(route, unread_notifications=count, can={}))
                    elements = Elements(html).items
                    ids = [a['id'] for _, a in elements if 'id' in a]
                    self.assertEqual(len(ids), len(set(ids)))
                    bells = [a for tag, a in elements if tag == 'a' and a.get('class') == 'notification-bell']
                    self.assertEqual(len(bells), 1)
                    self.assertEqual(bells[0]['href'], '/notifications/')
                    if count:
                        self.assertIn(str(count), bells[0]['aria-label'])
                    self.assertIn('class="profile-menu"', html)
                    self.assertIn('action="/accounts/logout/"', html)
                    self.assertIn('action="/i18n/setlang/"', html)
                    self.assertEqual(ids.count('language-select'), 1)
                    self.assertIn('data-nav-overlay hidden aria-hidden="true"', html)
                    self.assertNotIn(' style=', html)

    def test_user_identity_is_escaped_and_console_option_is_staff_only(self):
        context = self.context()
        context['user'].first_name = '<script>unsafe</script>'
        html = render_to_string('components/header_account.html', context)
        self.assertIn('&lt;script&gt;unsafe&lt;/script&gt;', html)
        self.assertNotIn('href="/manage/"', html)
        context['user'].is_staff = True
        html = render_to_string('components/header_account.html', context)
        self.assertIn('href="/manage/"', html)
