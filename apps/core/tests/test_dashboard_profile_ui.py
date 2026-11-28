"""UI-04 dashboard/profile rendering and read-only adapter contracts; no database access."""
from datetime import timedelta
from types import SimpleNamespace
from uuid import UUID
from unittest.mock import patch
from django.template.loader import render_to_string
from django.test import SimpleTestCase, RequestFactory
from django.utils import timezone, translation
from apps.core.views import MemberDashboardView
from .test_member_viewing_ui import MemberViewingUITests
from .test_public_auth_ui import Elements


class DashboardProfileUITests(SimpleTestCase):
    def context(self, path='/dashboard/'):
        context = MemberViewingUITests().context(path)
        context['user'].pk = UUID('00000000-0000-0000-0000-000000000004')
        context['user'].get_full_name = lambda: 'Amina Juma'
        context.update(csrf_token='test', nav='dashboard', restricted_demo=True,
            dashboard_plans=[{'name':'Published plan','code':'published','price_amount':7000,'duration_days':30,
                              'benefits':['Published benefit']}],
            dashboard_networks=[('MPESA','Vodacom M-Pesa')],
            summary={'wallet':{'wallet':True,'eligible':True,'balances':{'available':711,'restricted':False}},
                     'sponsored':{'enabled':True,'verified':7,'available':2,'review':1},
                     'rewards':{'enabled':True,'qualified':3,'simulated_total':321},
                     'membership':{'enabled':True,'subscription':{'plan_name':'Current plan','status':'ACTIVE',
                        'expires_at':timezone.now()+timedelta(days=10),'get_status_display':'Active'}},
                     'withdrawals':{'available':False,'destinations':1,'messages':['Withdrawals are not available yet']},
                     'payments':{'sandbox':True,'total':2}, 'activity':[]}, recent_videos=[])
        return context

    def test_four_summary_cards_use_real_context_and_clear_money_semantics(self):
        with translation.override('en'):
            html=render_to_string('members/dashboard.html', self.context())
        stats=[a for tag,a in Elements(html).items if tag=='section' and 'dashboard-stat' in a.get('class','').split()]
        self.assertEqual(len(stats),4)
        self.assertIn('TZS 711', html)
        self.assertIn('TZS 321', html)
        self.assertIn('7 verified views', html)
        self.assertIn('Current plan', html)
        self.assertIn('SIMULATED REWARDS', html)
        self.assertIn('NOT WITHDRAWABLE', html)
        self.assertIn('2 test payments', html)
        for sample in ('24,500','1,440','Silas','CC204589'):
            self.assertNotIn(sample,html)

    def test_no_wallet_is_not_presented_as_a_zero_balance(self):
        context=self.context()
        context['summary']['wallet']={}
        context['summary']['rewards']={'enabled':True}
        html=render_to_string('members/dashboard.html',context)
        self.assertNotIn('TZS 0',html)
        self.assertIn('Wallets are for verified member accounts',html)
        self.assertIn('No demo rewards yet',html)

    def test_locked_video_data_is_not_exposed_even_if_supplied(self):
        context=self.context()
        context.update(videos_locked=True,recent_videos=[{'title':'SECRET VIDEO','slug':'secret','thumbnail':'https://private.invalid/signed'}])
        html=render_to_string('members/dashboard.html',context)
        self.assertNotIn('SECRET VIDEO',html)
        self.assertNotIn('private.invalid',html)
        self.assertIn('Videos are available to members with an active membership',html)

    def test_feature_gates_and_referral_are_honest(self):
        context=self.context()
        context['features']={}
        html=render_to_string('members/dashboard.html',context)
        self.assertNotIn('Published plan',html)
        self.assertNotIn('href="/videos/"',html)
        self.assertNotIn('href="/ref',html)
        self.assertNotIn('data-copy-value',html)
        # REF-01: an account without referral eligibility gets an honest, disabled panel and no link.
        self.assertIn('Referral sharing is not available on your account.',html)
        self.assertIn('Destination support does not mean live payouts are enabled',html)

    def test_public_plan_actions_are_detail_links_and_ids_are_unique(self):
        html=render_to_string('members/dashboard.html',self.context())
        self.assertIn('href="/memberships/plans/published/"',html)
        self.assertNotIn('/published/choose/',html)
        elements=Elements(html).items
        ids=[a['id'] for _,a in elements if 'id' in a]
        self.assertEqual(len(ids),len(set(ids)))
        self.assertIn('class="dashboard-columns"',html)
        self.assertIn('class="dashboard-lower"',html)

    def test_profile_preserves_security_and_contact_actions(self):
        context=self.context('/accounts/')
        context.update(nav='profile',email_change_available=True,pending_email_change={'new_email':'new@example.test'},activity=[])
        context['user'].email='old@example.test'
        context['user'].email_verified=False
        context['user'].email_recovers_account=True
        html=render_to_string('accounts/profile.html',context)
        for route in ('/accounts/profile/edit/','/accounts/password-change/','/accounts/email-change/',
                      '/accounts/email-change/cancel/','/accounts/verify/resend/','/accounts/logout/','/notifications/'):
            self.assertIn(route,html)
        self.assertIn('csrfmiddlewaretoken',html)
        self.assertIn('never ask for your password',html)
        self.assertIn('account-settings-menu',html)
        self.assertIn('00000000-0000-0000-0000-000000000004',html)
        self.assertNotIn('notification_preferences',html)

    def test_read_only_plan_adapter_is_bounded_and_feature_gated(self):
        for enabled in (True,False):
            request=RequestFactory().get('/dashboard/')
            request.user=self.context()['user']
            view=MemberDashboardView();view.setup(request)
            with patch('apps.core.views.member_summary',return_value=SimpleNamespace(membership={})), \
                 patch('apps.core.views.features.is_enabled',side_effect=lambda key: key=='memberships' and enabled), \
                 patch('apps.accounts.services.email_change_available',return_value=True), \
                 patch('apps.memberships.models.MembershipPlan.objects.filter') as query:
                query.return_value.__getitem__.return_value=[]
                data=view.get_context_data()
                self.assertEqual(data['dashboard_plans'],[])
                if enabled:
                    query.assert_called_once_with(status='ACTIVE',is_public=True)
                    query.return_value.__getitem__.assert_called_once_with(slice(None,4))
                else: query.assert_not_called()
                self.assertTrue(data['dashboard_networks'])

    def test_swahili_labels_preserve_the_underlying_financial_values(self):
        with translation.override('sw'):
            html=render_to_string('members/dashboard.html',self.context())
            self.assertIn('Tazama Video Sasa',html)
            self.assertIn('Mialiko',html)
            self.assertIn('Kushiriki mialiko hakupatikani kwenye akaunti yako.',html)
            self.assertIn('TZS 711',html)
            self.assertIn('TZS 321',html)
