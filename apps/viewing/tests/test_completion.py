"""UI-DASH-03 closure: automatic completion of watch sessions.

The player sends its final "ended" event the moment the video finishes, usually within a second of
the last heartbeat. The 1-second minimum interval used to refuse it (HTTP 429), leaving the session
open until the member pressed "Finish session" or it expired. Only that terminal event is exempt
from the interval; every other limit and evidence rule still applies.
"""
import json
import uuid

from django.test import Client, override_settings
from django.urls import reverse

from apps.audit.models import AuditEvent
from apps.rewards import services as rewards
from apps.rewards.models import DemoReward
from apps.viewing import services

from .helpers import ViewingTestCase, WatchSession, make_user

St = WatchSession.State
Out = WatchSession.Outcome


class CompletionTests(ViewingTestCase):
    def play(self, session, until_s, step=10):
        """Honest heartbeats up to ``until_s``; returns the last sequence number used."""
        seq = 1
        self.event(session, seq, 0)
        position = 0
        while position < until_s:
            seq += 1
            position = min(until_s, position + step)
            self.event(session, seq, position, after=step)
        return seq

    def test_ended_right_after_a_heartbeat_completes_the_session(self):
        session = self.start()
        seq = self.play(session, 95)
        result = self.event(session, seq + 1, 95, state="ended", after=0.2)
        self.assertEqual((result.accepted, result.http_status), (True, 200))
        session.refresh_from_db()
        self.assertEqual((session.state, session.outcome, session.qualifying_slot),
                         (St.FINALIZED, Out.VERIFIED_EVIDENCE, 1))
        self.assertEqual(list(services.verified_views(self.campaign)), [session])

    def test_ended_with_no_gap_at_all_is_accepted(self):
        session = self.start()
        seq = self.play(session, 95)
        result = self.event(session, seq + 1, 95, state="ended")  # Same server instant.
        self.assertTrue(result.accepted)
        session.refresh_from_db()
        self.assertEqual(session.outcome, Out.VERIFIED_EVIDENCE)

    def test_repeated_completion_creates_one_view_and_one_reward_decision(self):
        session = self.start()
        seq = self.play(session, 95)
        self.event(session, seq + 1, 95, state="ended", after=0.2)
        retry_same_seq = self.event(session, seq + 1, 95, state="ended", after=0.1)
        retry_next_seq = self.event(session, seq + 2, 95, state="ended", after=0.1)
        for result in (retry_same_seq, retry_next_seq):
            self.assertEqual((result.accepted, result.code, result.http_status), (False, "session_closed", 409))
        finished = services.finish_session(self.member, session.pk)
        self.assertEqual((finished.outcome, finished.qualifying_slot), (Out.VERIFIED_EVIDENCE, 1))
        self.assertEqual(WatchSession.objects.filter(qualifying_slot__isnull=False).count(), 1)
        self.assertEqual(AuditEvent.objects.filter(action="viewing.session.finalized").count(), 1)
        first = rewards.evaluate_session(session.pk)
        second = rewards.evaluate_session(session.pk)
        if first is not None:
            self.assertEqual(first.pk, second.pk)
        self.assertLessEqual(DemoReward.objects.filter(session=session).count(), 1)

    def test_rapid_heartbeats_are_still_limited(self):
        session = self.start()
        self.event(session, 1, 0)
        for state in ("playing", "paused"):
            result = self.event(session, 2, 0, state=state, after=0.2)
            self.assertEqual((result.accepted, result.http_status), (False, 429))

    @override_settings(VIEWING_MAX_EVENTS_PER_MINUTE=2)
    def test_ended_is_still_subject_to_the_per_minute_limit(self):
        session = self.start()
        self.event(session, 1, 0)
        self.event(session, 2, 10, after=10)
        result = self.event(session, 3, 10, state="ended", after=0.2)
        self.assertEqual((result.accepted, result.http_status), (False, 429))
        session.refresh_from_db()
        self.assertEqual(session.state, St.IN_PROGRESS)

    def test_early_exit_does_not_qualify(self):
        session = self.start()
        seq = self.play(session, 30)
        self.event(session, seq + 1, 30, state="ended", after=0.2)
        session.refresh_from_db()
        self.assertEqual(session.state, St.FINALIZED)
        self.assertNotEqual(session.outcome, Out.VERIFIED_EVIDENCE)
        self.assertIsNone(session.qualifying_slot)
        self.assertEqual(list(services.verified_views(self.campaign)), [])

    def test_ended_at_a_skipped_ahead_position_does_not_qualify(self):
        session = self.start()
        seq = self.play(session, 20)
        self.event(session, seq + 1, 95, state="ended", after=0.2)
        session.refresh_from_db()
        self.assertNotEqual(session.outcome, Out.VERIFIED_EVIDENCE)
        self.assertIsNone(session.qualifying_slot)

    def test_interrupted_playback_expires_without_a_view(self):
        session = self.start()
        self.play(session, 40)
        self.clock.advance(400)  # Network lost; no "ended" ever arrives.
        services.expire_sessions()
        session.refresh_from_db()
        self.assertNotEqual(session.outcome, Out.VERIFIED_EVIDENCE)
        self.assertIsNone(session.qualifying_slot)

    def test_expired_session_refuses_a_late_ended_event(self):
        session = self.start()
        self.event(session, 1, 0)
        self.clock.advance(4 * 3600)
        result = self.event(session, 2, 95, state="ended", after=0.2)
        self.assertEqual((result.accepted, result.code), (False, "session_closed"))

    def test_other_members_cannot_complete_a_session(self):
        session = self.start()
        seq = self.play(session, 95)
        other = make_user("other-completion@x.example")
        with self.assertRaises(services.NotFound):
            self.event(session, seq + 1, 95, state="ended", member=other, after=0.2)
        with self.assertRaises(services.NotFound):
            services.finish_session(other, session.pk)
        session.refresh_from_db()
        self.assertEqual(session.state, St.IN_PROGRESS)


class CompletionWebTests(ViewingTestCase):
    def setUp(self):
        super().setUp()
        self.session = self.start()
        self.url = reverse("viewing:events", args=[self.session.pk])

    def post(self, client, payload):
        return client.post(self.url, json.dumps(payload), content_type="application/json")

    def test_rapid_ended_over_http_closes_the_session(self):
        self.client.force_login(self.member)
        seq, position = 1, 0
        self.post(self.client, {"seq": seq, "position_ms": 0, "state": "playing"})
        while position < 95:
            seq, position = seq + 1, min(95, position + 10)
            self.clock.advance(10)
            self.post(self.client, {"seq": seq, "position_ms": position * 1000, "state": "playing"})
        self.clock.advance(0.3)
        response = self.post(self.client, {"seq": seq + 1, "position_ms": 95_000, "state": "ended"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual((response.json()["accepted"], response.json()["outcome"]), (True, "VERIFIED_EVIDENCE"))
        again = self.post(self.client, {"seq": seq + 1, "position_ms": 95_000, "state": "ended"})
        self.assertEqual(again.status_code, 409)
        finish = self.client.post(reverse("viewing:finish", args=[self.session.pk]), HTTP_ACCEPT="application/json")
        self.assertEqual(finish.json()["outcome"], "VERIFIED_EVIDENCE")
        self.assertEqual(WatchSession.objects.filter(qualifying_slot__isnull=False).count(), 1)

    def test_unauthorized_completion_requests(self):
        anonymous = Client()
        ended = {"seq": 1, "position_ms": 95_000, "state": "ended"}
        self.assertIn(self.post(anonymous, ended).status_code, (302, 403))
        self.client.force_login(make_user("stranger-completion@x.example"))
        self.assertEqual(self.post(self.client, ended).status_code, 404)
        missing = reverse("viewing:events", args=[uuid.uuid4()])
        self.assertEqual(self.client.post(missing, json.dumps(ended), content_type="application/json").status_code, 404)
        csrf_client = Client(enforce_csrf_checks=True)
        csrf_client.force_login(self.member)
        self.assertEqual(self.post(csrf_client, ended).status_code, 403)
        self.session.refresh_from_db()
        self.assertEqual((self.session.event_count, self.session.state), (0, St.CREATED))
