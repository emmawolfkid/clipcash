"""UI-DASH-03: administrator-managed advertising, advertiser CSV export, localized notifications and
explicit preview states. Real views and services; nothing is bypassed or simulated."""
import csv
import io
from datetime import timedelta

from django.conf import settings
from django.contrib.auth.models import Permission
from django.test import override_settings
from django.urls import reverse
from django.utils import timezone, translation

from apps.advertising import services
from apps.advertising.models import AdvertiserInvitation, Campaign, Organization
from apps.audit.models import AuditEvent
from apps.notifications import inbox
from apps.notifications.models import Notification
from apps.viewing.tests.helpers import ViewingTestCase, make_user, User

FORMAT = "%Y-%m-%dT%H:%M"


def local(moment):
    return timezone.localtime(moment).strftime(FORMAT)


@override_settings(VIEWING_MAX_STARTS_PER_HOUR=100)
class StaffManagedAdvertisingTests(ViewingTestCase):
    def setUp(self):
        super().setUp()
        self.reviewer = make_user("second.reviewer@example.com", role=User.Role.SUPER_ADMIN)
        self.creative = self.published_video("Staff-managed clip")

    def as_user(self, user, language="en"):
        self.client.force_login(user)
        self.client.cookies[settings.LANGUAGE_COOKIE_NAME] = language

    def staff_form(self, organization, **values):
        now = timezone.now()
        data = {"organization": organization.pk, "name": "Client summer advert", "description": "Made by staff.",
                "video": self.creative.pk, "starts_at": local(now - timedelta(hours=1)),
                "ends_at": local(now + timedelta(days=7)), "max_views_per_member": 1,
                "demo_budget_amount": 40_000, "demo_reward_per_view": 100}
        data.update(values)
        return data

    def test_workflow_b_staff_creates_for_client_and_another_reviewer_publishes(self):
        # Register a client organization that has no account yet; a second reviewer verifies it.
        self.as_user(self.root)
        response = self.client.post(reverse("administration:advertiser_create"), {
            "display_name": "Client Foods", "legal_name": "Client Foods Ltd", "contact_email": "ads@client.example",
            "country": "TZ"})
        organization = Organization.objects.get(display_name="Client Foods")
        self.assertRedirects(response, reverse("administration:advertiser_review", args=[organization.pk]))
        self.assertEqual(organization.created_by, self.root)
        self.assertFalse(organization.memberships.exists())  # No impersonated owner.
        self.client.post(reverse("administration:advertiser_review", args=[organization.pk]),
                         {"action": "approve", "expected_status": "PENDING"})
        organization.refresh_from_db()
        self.assertEqual(organization.verification_status, "PENDING")  # Creator cannot verify it.
        self.as_user(self.reviewer)
        self.client.post(reverse("administration:advertiser_review", args=[organization.pk]),
                         {"action": "approve", "expected_status": "PENDING"})
        organization.refresh_from_db()
        self.assertTrue(organization.is_approved)

        # Staff create the campaign (the video is authorized for the client on save, audited).
        self.as_user(self.root)
        response = self.client.post(reverse("administration:campaign_create"), self.staff_form(organization))
        campaign = Campaign.objects.get(name="Client summer advert")
        self.assertRedirects(response, reverse("administration:campaign_review", args=[campaign.pk]))
        self.assertEqual((campaign.organization, campaign.created_by, campaign.status),
                         (organization, self.root, Campaign.Status.DRAFT))
        self.assertTrue(services.video_authorized(organization, self.creative))
        created = AuditEvent.objects.get(action="campaign.created", target_id=str(campaign.pk))
        self.assertEqual((created.actor_id, created.metadata.get("on_behalf")), (self.root.pk, True))
        self.assertNotIn("Client summer advert", self.client.get(reverse("viewing:list")).content.decode())

        # Submit for review; the creating staff member cannot approve (four eyes).
        self.client.post(reverse("administration:campaign_review", args=[campaign.pk]), {"action": "submit"})
        campaign.refresh_from_db()
        self.assertEqual(campaign.status, Campaign.Status.SUBMITTED)
        page = self.client.get(reverse("administration:campaign_review", args=[campaign.pk]))
        self.assertContains(page, "a different reviewer must approve it")
        self.assertNotContains(page, 'value="approve"')
        self.client.post(reverse("administration:campaign_review", args=[campaign.pk]),
                         {"action": "approve", "expected_revision": campaign.revision})
        campaign.refresh_from_db()
        self.assertEqual(campaign.status, Campaign.Status.SUBMITTED)
        self.assertTrue(AuditEvent.objects.filter(action="advertiser.action.denied",
                                                  metadata__reason="self_approval").exists())

        # A second reviewer approves; eligible members now see it like any advertiser campaign.
        self.as_user(self.reviewer)
        self.client.post(reverse("administration:campaign_review", args=[campaign.pk]),
                         {"action": "approve", "expected_revision": campaign.revision})
        campaign.refresh_from_db()
        self.assertEqual(campaign.status, Campaign.Status.ACTIVE)
        self.as_user(self.member)
        self.assertIn("Client summer advert", self.client.get(reverse("viewing:list")).content.decode())
        self.assertContains(self.client.get(reverse("dashboard")), "Client summer advert")

        # Staff pause and resume it from the console like any campaign.
        self.as_user(self.reviewer)
        self.client.post(reverse("administration:campaign_review", args=[campaign.pk]),
                         {"action": "pause", "reason": "Checking the creative"})
        campaign.refresh_from_db()
        self.assertEqual(campaign.status, Campaign.Status.PAUSED)
        self.as_user(self.member)
        self.assertNotIn("Client summer advert", self.client.get(reverse("viewing:list")).content.decode())

    def test_staff_without_the_permission_cannot_create_or_register(self):
        content = make_user("content.staff@example.com", role=User.Role.CONTENT_MANAGER)
        self.as_user(content)
        for name in ("administration:campaign_create", "administration:advertiser_create"):
            self.assertEqual(self.client.get(reverse(name)).status_code, 403, name)
        self.assertEqual(self.client.post(reverse("administration:campaign_create"),
                                          self.staff_form(self.org)).status_code, 403)
        self.assertFalse(Campaign.objects.filter(name="Client summer advert").exists())
        with self.assertRaises(services.NotAuthorized):
            services.create_campaign(content, self.org, **{k: v for k, v in self.campaign_values().items()})

    def test_manage_permission_without_video_rights_cannot_authorize_a_video(self):
        staff = make_user("campaign.staff@example.com", role=User.Role.CONTENT_MANAGER)
        staff.user_permissions.add(Permission.objects.get(codename="manage_campaigns",
                                                          content_type__app_label="advertising"))
        staff = User.objects.get(pk=staff.pk)  # Fresh permission cache.
        self.as_user(staff)
        page = self.client.post(reverse("administration:campaign_create"), self.staff_form(self.org))
        self.assertContains(page, "not allowed to authorize this video")
        self.assertFalse(Campaign.objects.filter(name="Client summer advert").exists())
        # With an already authorized video the same staff member can create the draft.
        self.client.post(reverse("administration:campaign_create"), self.staff_form(self.org, video=self.ad_video.pk))
        self.assertTrue(Campaign.objects.filter(name="Client summer advert", created_by=staff).exists())

    def test_staff_invite_the_client_owner(self):
        self.as_user(self.root)
        self.client.post(reverse("administration:advertiser_review", args=[self.org.pk]),
                         {"action": "invite", "email": "boss@client.example", "role": "OWNER"})
        invitation = AdvertiserInvitation.objects.get(organization=self.org, email="boss@client.example")
        self.assertEqual((invitation.role, invitation.invited_by), ("OWNER", self.root))

    def test_queue_tabs_and_create_entry_points(self):
        self.as_user(self.root)
        page = self.client.get(reverse("administration:campaigns")).content.decode()
        for text in ("Awaiting review", "Active", "Paused", "Drafts", "Completed", reverse("administration:campaign_create")):
            self.assertIn(text, page)
        detail = self.client.get(reverse("administration:advertiser_review", args=[self.org.pk])).content.decode()
        self.assertIn(f'{reverse("administration:campaign_create")}?organization={self.org.pk}', detail)
        form = self.client.get(reverse("administration:campaign_create") + f"?organization={self.org.pk}")
        self.assertContains(form, f'<option value="{self.org.pk}" selected>')


class AdvertiserExportTests(ViewingTestCase):
    def setUp(self):
        super().setUp()
        self.campaign.name  # Fixture campaign is ACTIVE for self.org.

    def export(self, user, org=None, **data):
        self.client.force_login(user)
        return self.client.post(reverse("advertising:reports_export", args=[(org or self.org).pk]), data)

    def test_owner_exports_masked_formula_safe_csv_and_it_is_audited(self):
        services.create_campaign(self.owner, self.org, **self.campaign_values(name="=HYPERLINK(\"http://x\")"))
        session = self.start()
        self.watch(session, 95)
        response = self.export(self.owner)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "text/csv; charset=utf-8")
        self.assertEqual(response["Cache-Control"], "no-store")
        rows = list(csv.reader(io.StringIO(response.content.decode().lstrip("﻿"))))
        self.assertEqual(rows[0][:3], ["Advertiser", "Campaign", "Status"])
        names = {row[1] for row in rows[1:]}
        self.assertIn("'=HYPERLINK(\"http://x\")", names)  # Neutralised for spreadsheets.
        launch = next(row for row in rows[1:] if row[1] == self.campaign.name)
        self.assertIn("fewer than", launch[7])  # One verified view stays masked (D12).
        self.assertNotIn(self.member.email, response.content.decode())
        event = AuditEvent.objects.get(action="reports.exported", target_id=str(self.org.pk))
        self.assertEqual((event.actor_id, event.metadata["report"]), (self.owner.pk, "advertiser"))

    def test_export_isolation_and_method(self):
        self.client.force_login(self.owner)
        self.assertEqual(self.client.get(reverse("advertising:reports_export", args=[self.org.pk])).status_code, 405)
        rival = make_user("rival@example.com")
        rival_org = services.create_organization(rival, display_name="Rival", legal_name="Rival Ltd",
                                                 contact_email="ads@rival.example", country="TZ")
        self.assertEqual(self.export(rival).status_code, 404)  # Another advertiser's data: not found.
        self.assertEqual(self.export(self.member).status_code, 404)  # Members are not on the team.
        self.assertEqual(self.export(self.owner, org=rival_org).status_code, 404)
        self.assertFalse(AuditEvent.objects.filter(action="reports.exported").exists())

    def test_invalid_period_returns_to_the_report(self):
        response = self.export(self.owner, **{"from": "2026-13-01", "to": "x"})
        self.assertRedirects(response, reverse("advertising:reports", args=[self.org.pk]) + "?from=2026-13-01&to=x",
                             fetch_redirect_response=False)


class LocalizedNotificationTests(ViewingTestCase):
    def test_new_notices_follow_the_reader_language_and_old_ones_keep_their_text(self):
        legacy = Notification.objects.create(recipient=self.owner, category="ADVERTISING", kind="legacy",
                                             title="Stored English title", message="Stored message",
                                             dedupe_key="legacy:1")
        services.pause_campaign(self.root, self.campaign, reason="Review")
        notice = Notification.objects.get(recipient=self.owner, kind="campaign.paused")
        self.assertEqual(notice.title, "Campaign paused by ClipCash")  # English stored as sent.
        self.assertEqual(notice.params, {"name": self.campaign.name})
        self.client.force_login(self.owner)
        self.client.cookies[settings.LANGUAGE_COOKIE_NAME] = "sw"
        page = self.client.get(reverse("notifications:inbox")).content.decode()
        self.assertIn("Kampeni imesitishwa na ClipCash", page)
        self.assertIn(f"“{self.campaign.name}” imesitishwa.", page)  # Name stays as recorded.
        self.assertIn("Stored English title", page)
        legacy.refresh_from_db()
        notice.refresh_from_db()
        self.assertEqual(notice.title, "Campaign paused by ClipCash")  # Display never rewrites history.
        self.client.cookies[settings.LANGUAGE_COOKIE_NAME] = "en"
        self.assertContains(self.client.get(reverse("notifications:inbox")), "Campaign paused by ClipCash")

    def test_translated_labels_and_bad_templates_fall_back(self):
        inbox.notify(self.member, "test.label", category="SUPPORT", title="Category: {category__t}.",
                     params={"category__t": "Account and security"}, dedupe_key="t:1")
        broken = Notification.objects.create(recipient=self.member, category="SUPPORT", kind="t", dedupe_key="t:2",
                                             title="Fallback title", title_template="Missing {value}", params={})
        with translation.override("sw"):
            first = inbox.localize([Notification.objects.get(dedupe_key="t:1"), broken])
        self.assertEqual(first[0].title, "Kundi: Akaunti na usalama.")
        self.assertEqual(first[1].title, "Fallback title")

    def test_dates_use_the_reader_language_month_names(self):
        from datetime import datetime, timezone as dt_timezone

        start = datetime(2026, 11, 4, 15, 0, tzinfo=dt_timezone.utc)  # 18:00 in Dar es Salaam.
        end = datetime(2027, 1, 4, 15, 0, tzinfo=dt_timezone.utc)
        inbox.notify(self.member, "membership.activated", category="MEMBERSHIP",
                     title="Your {plan} membership is active", message="Access runs from {start} until {end}.",
                     params={"plan": "Gold", "start": start, "end": end}, dedupe_key="dates:1")
        notice = Notification.objects.get(dedupe_key="dates:1")
        # Stored (history and email) text is the English rendering, unchanged in format.
        self.assertEqual(notice.message, "Access runs from 4 Nov 2026, 18:00 EAT until 4 Jan 2027, 18:00 EAT.")
        self.assertEqual(notice.params, {"plan": "Gold", "start": start.isoformat(), "end": end.isoformat(),
                                         inbox.DATES_KEY: ["start", "end"]})
        # A notice stored before dates were recorded as ISO keeps its recorded text in every language.
        legacy = Notification.objects.create(
            recipient=self.member, category="MEMBERSHIP", kind="membership.activated", dedupe_key="dates:2",
            title="Your Gold membership is active", message="Access runs until 4 Nov 2026, 18:00 EAT.",
            title_template="Your {plan} membership is active", message_template="Access runs until {end}.",
            params={"plan": "Gold", "end": "4 Nov 2026, 18:00 EAT"})
        with translation.override("sw"):
            sw_new, sw_old = inbox.localize([Notification.objects.get(dedupe_key="dates:1"), legacy])
        self.assertEqual(sw_new.message, "Ufikiaji unaanza 4 Novemba 2026, 18:00 EAT hadi 4 Januari 2027, 18:00 EAT.")
        self.assertEqual(sw_old.message, "Ufikiaji unaendelea hadi 4 Nov 2026, 18:00 EAT.")
        with translation.override("en"):
            en_new = inbox.localize([Notification.objects.get(dedupe_key="dates:1")])[0]
        self.assertEqual(en_new.message,
                         "Access runs from 4 November 2026, 18:00 EAT until 4 January 2027, 18:00 EAT.")
        notice.refresh_from_db()
        self.assertEqual(notice.message, "Access runs from 4 Nov 2026, 18:00 EAT until 4 Jan 2027, 18:00 EAT.")

    def test_membership_notice_dates_render_in_swahili(self):
        from django.utils.formats import date_format

        from apps.memberships import services as memberships
        from apps.memberships.models import Subscription

        subscription = Subscription.objects.filter(member=self.member).first()
        memberships._notify_member(subscription, Subscription.Status.SUSPENDED, Subscription.Status.ACTIVE, None)
        notice = Notification.objects.get(recipient=self.member, kind="membership.reinstated")
        self.assertEqual(notice.params[inbox.DATES_KEY], ["end"])
        local_end = inbox._site_local(subscription.expires_at)
        self.assertIn(f"{local_end.day} {local_end:%b %Y}", notice.message)  # Stored English text.
        self.client.force_login(self.member)
        self.client.cookies[settings.LANGUAGE_COOKIE_NAME] = "sw"
        page = self.client.get(reverse("notifications:inbox")).content.decode()
        with translation.override("sw"):
            expected = date_format(local_end, "j F Y, H:i")
        self.assertIn(expected, page)


class PreviewStateTests(ViewingTestCase):
    def test_unconfirmed_bunny_video_shows_an_explanation_not_a_player(self):
        type(self.campaign.video).objects.filter(pk=self.campaign.video_id).update(last_synced_at=None)
        self.campaign.refresh_from_db()  # A video Bunny has not confirmed (never synced).
        reason, playback = services.preview_playback(self.owner, self.campaign)
        self.assertEqual((reason, playback), ("video_not_confirmed", None))
        self.client.force_login(self.owner)
        page = self.client.get(reverse("advertising:campaign", args=[self.org.pk, self.campaign.pk]))
        self.assertContains(page, "has not confirmed this video yet")
        self.assertNotContains(page, "<iframe")

    def test_synced_video_gets_the_signed_player(self):
        type(self.campaign.video).objects.filter(pk=self.campaign.video_id).update(last_synced_at=timezone.now())
        self.campaign.refresh_from_db()
        reason, playback = services.preview_playback(self.owner, self.campaign)
        self.assertEqual((reason, playback.kind), ("ok", "iframe"))
        self.assertIn("token=", playback.url)
