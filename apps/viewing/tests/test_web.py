import json
import uuid

from django.test import Client
from django.urls import reverse

from apps.videos.tests.helpers import API_KEY, TOKEN_KEY
from apps.viewing import services

from .helpers import ViewingTestCase, WatchSession, make_user


class MemberPagesTests(ViewingTestCase):
    def test_list_start_and_session_page(self):
        self.client.force_login(self.member)
        listing = self.client.get(reverse("viewing:list"))
        self.assertContains(listing, self.campaign.name)
        self.assertContains(listing, "Verified views are not rewards")
        start = self.client.post(reverse("viewing:campaign", args=[self.campaign.pk]))
        session = WatchSession.objects.get()
        self.assertRedirects(start, reverse("viewing:session", args=[session.pk]))
        page = self.client.get(reverse("viewing:session", args=[session.pk]))
        self.assertContains(page, 'id="watch-frame"')
        self.assertContains(page, "token=")
        self.assertContains(page, reverse("viewing:events", args=[session.pk]))
        self.assertNotContains(page, API_KEY)
        self.assertNotContains(page, TOKEN_KEY)
        self.assertEqual(self.client.get(reverse("viewing:history")).status_code, 200)

    def test_denied_start_shows_message(self):
        self.client.force_login(make_user("nomember@x.example"))
        response = self.client.post(reverse("viewing:campaign", args=[self.campaign.pk]), follow=True)
        self.assertContains(response, "An active membership is needed")
        self.assertFalse(WatchSession.objects.exists())
        self.assertEqual(self.client.get(reverse("viewing:campaign", args=[self.campaign.pk])).status_code, 200)

    def test_anonymous_and_unverified(self):
        self.assertEqual(self.client.get(reverse("viewing:list")).status_code, 302)
        self.client.force_login(make_user("u@x.example", verified=False))
        self.assertRedirects(self.client.get(reverse("viewing:list")), reverse("accounts:verification_required"))

    def test_finished_session_shows_outcome(self):
        session = self.watch(self.start(), 95)
        self.client.force_login(self.member)
        page = self.client.get(reverse("viewing:session", args=[session.pk]))
        self.assertContains(page, "View verified")
        self.assertNotContains(page, 'id="watch-panel"')


class EvidenceEndpointTests(ViewingTestCase):
    def setUp(self):
        super().setUp()
        self.session = self.start()
        self.url = reverse("viewing:events", args=[self.session.pk])

    def post(self, client, body):
        return client.post(self.url, json.dumps(body) if not isinstance(body, str) else body,
                           content_type="application/json")

    def test_json_protocol(self):
        self.client.force_login(self.member)
        response = self.post(self.client, {"seq": 1, "position_ms": 0, "state": "playing"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["state"], "IN_PROGRESS")
        self.clock.advance(10)
        body = self.post(self.client, {"seq": 2, "position_ms": 10_000, "state": "playing"}).json()
        self.assertEqual((body["accepted"], body["credited_seconds"], body["required_seconds"]), (True, 10, 85))
        self.clock.advance(2)
        self.assertEqual(self.post(self.client, "not json").status_code, 400)
        self.assertEqual(self.post(self.client, "x" * 2000).status_code, 413)
        self.assertEqual(self.client.get(self.url).status_code, 405)

    def test_ownership_and_csrf(self):
        stranger = make_user("s@x.example")
        self.client.force_login(stranger)
        self.assertEqual(self.post(self.client, {"seq": 1, "position_ms": 0, "state": "playing"}).status_code, 404)
        missing = reverse("viewing:events", args=[uuid.uuid4()])
        self.assertEqual(self.client.post(missing, "{}", content_type="application/json").status_code, 404)
        csrf_client = Client(enforce_csrf_checks=True)
        csrf_client.force_login(self.member)
        self.assertEqual(self.post(csrf_client, {"seq": 1, "position_ms": 0, "state": "playing"}).status_code, 403)
        self.assertEqual(csrf_client.post(reverse("viewing:finish", args=[self.session.pk])).status_code, 403)
        self.session.refresh_from_db()
        self.assertEqual((self.session.event_count, self.session.state), (0, "CREATED"))

    def test_finish_endpoint(self):
        self.client.force_login(self.member)
        url = reverse("viewing:finish", args=[self.session.pk])
        self.assertEqual(self.client.get(url).status_code, 405)
        response = self.client.post(url, HTTP_ACCEPT="application/json")
        self.assertEqual(response.json()["outcome"], "EXPIRED")


class ConsoleTests(ViewingTestCase):
    def test_review_console(self):
        session = self.watch(self.start(), 95)
        self.client.force_login(self.manager)
        self.assertEqual(self.client.get(reverse("administration:viewing")).status_code, 403)
        self.client.force_login(self.root)
        listing = self.client.get(reverse("administration:viewing"), {"outcome": "VERIFIED_EVIDENCE",
                                                                      "member": "member@"})
        self.assertContains(listing, self.member.email)
        detail = self.client.get(reverse("administration:viewing_review", args=[session.pk]))
        self.assertContains(detail, "viewing.session.finalized")
        self.assertContains(detail, "not proof of attention")
        response = self.client.post(reverse("administration:viewing_review", args=[session.pk]),
                                    {"decision": "reject", "expected_status": "NONE", "reason": "Not pending"},
                                    follow=True)
        self.assertEqual(response.status_code, 200)
        session.refresh_from_db()
        self.assertEqual(session.outcome, "VERIFIED_EVIDENCE")  # Only sessions awaiting review can change.
        viewing = self.client.get(reverse("administration:overview")).context["sections"]["viewing"]
        self.assertEqual(viewing["outcomes"]["VERIFIED_EVIDENCE"], 1)

    def test_list_has_no_n_plus_one(self):
        from django.db import connection
        from django.test.utils import CaptureQueriesContext

        self.watch(self.start(), 95)
        self.client.force_login(self.root)

        def count():
            with CaptureQueriesContext(connection) as queries:
                self.client.get(reverse("administration:viewing"))
            return len(queries)

        count()
        baseline = count()
        for index in range(4):
            member = make_user(f"n{index}@x.example")
            self.active_membership(member=member)
            services.finish_session(member, self.start(member=member).pk)
        self.assertEqual(count(), baseline)
