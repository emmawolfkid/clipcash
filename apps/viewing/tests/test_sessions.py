from django.db import DatabaseError, IntegrityError, transaction
from django.test import override_settings

from apps.advertising import services as advertising
from apps.advertising.models import AdvertiserMembership
from apps.audit.models import AuditEvent
from apps.core.models import SiteConfiguration
from apps.memberships import services as memberships
from apps.videos import services as videos
from apps.videos.models import Video
from apps.viewing import services

from .helpers import ViewingTestCase, WatchSession, enable_advertisers, make_user

St = WatchSession.State
Out = WatchSession.Outcome


class StartTests(ViewingTestCase):
    def assert_denied(self, reason, member=None, campaign=None):
        with self.assertRaises(services.Denied) as caught:
            self.start(member, campaign)
        self.assertEqual(caught.exception.code, reason)
        self.assertTrue(AuditEvent.objects.filter(action="viewing.session.denied", metadata__reason=reason).exists())

    def test_eligibility_is_enforced(self):
        self.assert_denied("no_membership", make_user("nomember@x.example"))
        self.assert_denied("email_unverified", make_user("unverified@x.example", verified=False))
        from django.contrib.auth.models import AnonymousUser

        self.assert_denied("not_authenticated", AnonymousUser())
        gold = self.make_plan("gold", price=9000)
        targeted = self.running_campaign(name="Gold only", target_plans=[gold])
        self.assert_denied("plan_not_targeted", campaign=targeted)
        AdvertiserMembership.objects.create(organization=self.org, user=self.member, role="MANAGER")
        self.assert_denied("own_campaign")
        self.assertFalse(WatchSession.objects.exists())

    def test_campaign_advertiser_video_and_feature_states(self):
        subscription = self.member.subscriptions.get()
        memberships.suspend(self.root, subscription, "ADMIN_REVIEW")
        self.assert_denied("membership_suspended")
        memberships.reinstate(self.root, subscription)
        advertising.review_organization(self.root, self.org, "suspend", expected_status="APPROVED", reason="Check")
        self.assert_denied("organization_not_approved")
        advertising.review_organization(self.root, self.org, "reinstate", expected_status="SUSPENDED")
        videos.unpublish(self.manager, self.ad_video)
        self.assert_denied("video_unavailable")
        videos.publish(self.manager, self.ad_video)
        SiteConfiguration.objects.filter(id=1).update(videos_enabled=False)
        from django.core.cache import cache

        cache.clear()
        self.assert_denied("feature_disabled")
        SiteConfiguration.objects.filter(id=1).update(videos_enabled=True)
        enable_advertisers(False)
        self.assert_denied("feature_disabled")

    def test_start_is_not_a_view_and_is_idempotent(self):
        session = self.start()
        self.assertEqual((session.state, session.outcome, session.qualifying_slot), (St.CREATED, Out.PENDING, None))
        self.assertEqual((session.campaign_revision, session.video_id, session.required_ms),
                         (self.campaign.revision, self.ad_video.pk, 85_500))  # 90 % of 95 s.
        self.assertEqual(self.start().pk, session.pk)  # Second tab / double click.
        self.assertEqual(WatchSession.objects.count(), 1)
        self.assertEqual(AuditEvent.objects.filter(action="viewing.session.started").count(), 1)
        with self.assertRaises(IntegrityError), transaction.atomic():
            WatchSession.objects.create(member=self.member, campaign=self.campaign, campaign_revision=1,
                                        video=self.ad_video, start_deadline=session.start_deadline,
                                        expires_at=session.expires_at)

    def test_other_members_cannot_use_a_session(self):
        session = self.start()
        other = make_user("other@x.example")
        with self.assertRaises(services.NotFound):
            services.session_for(other, session.pk)
        with self.assertRaises(services.NotFound):
            self.event(session, 1, 0, member=other)
        with self.assertRaises(services.NotFound):
            services.finish_session(other, session.pk)

    def test_new_campaign_session_ends_the_previous_one(self):
        first = self.start()
        self.event(first, 1, 0)
        second_campaign = self.running_campaign(name="Second")
        second = self.start(campaign=second_campaign)
        first.refresh_from_db()
        self.assertEqual((first.state, first.outcome), (St.FINALIZED, Out.INSUFFICIENT_EVIDENCE))
        self.assertEqual(second.state, St.CREATED)

    @override_settings(VIEWING_MAX_STARTS_PER_HOUR=2)
    def test_rapid_attempts_are_limited(self):
        self.start()
        services.finish_session(self.member, WatchSession.objects.get().pk)
        self.clock.advance(1)
        self.start()
        services.finish_session(self.member, WatchSession.objects.latest("created_at").pk)
        self.assert_denied("too_many_attempts")


class EvidenceTests(ViewingTestCase):
    def test_honest_viewing_is_verified_once(self):
        session = self.watch(self.start(), 95)
        self.assertEqual((session.state, session.outcome, session.qualifying_slot), (St.FINALIZED,
                                                                                     Out.VERIFIED_EVIDENCE, 1))
        self.assertGreaterEqual(session.credited_ms, 85_500)
        self.assertEqual(session.flags, [])
        again = services.finish_session(self.member, session.pk)  # Repeated completion: no change.
        self.assertEqual(again.qualifying_slot, 1)
        self.assertEqual(AuditEvent.objects.filter(action="viewing.session.finalized").count(), 1)
        with self.assertRaises(services.Denied) as caught:  # views_per_member = 1
            self.start()
        self.assertEqual(caught.exception.code, "limit_reached")
        self.assertEqual(list(services.verified_views(self.campaign)), [session])

    def test_views_per_member_two(self):
        campaign = self.running_campaign(name="Twice", max_views_per_member=2)
        first = self.watch(self.start(campaign=campaign), 95)
        second = self.watch(self.start(campaign=campaign), 95)
        self.assertEqual((first.qualifying_slot, second.qualifying_slot), (1, 2))
        with self.assertRaises(services.Denied):
            self.start(campaign=campaign)

    def test_seeking_ahead_does_not_verify(self):
        session = self.start()
        self.event(session, 1, 0)
        self.event(session, 2, 10, after=10)
        self.event(session, 3, 90, after=2)  # Jump of 80 s in 2 s.
        self.event(session, 4, 95, state="ended", after=5)
        session.refresh_from_db()
        self.assertEqual((session.outcome, session.reasons), (Out.INSUFFICIENT_EVIDENCE, ["watch_time_short"]))
        self.assertIn("large_seeks", session.flags)
        self.assertLess(session.credited_ms, 20_000)

    def test_fast_playback_is_capped_by_server_time_and_flagged(self):
        session = self.watch(self.start(), 95, step=10, speed=1.5)  # Claims 15 s per 10 s.
        self.assertIn("impossible_speed", session.flags)
        self.assertLessEqual(session.credited_ms, 70_000)
        self.assertEqual(session.outcome, Out.INSUFFICIENT_EVIDENCE)

    def test_pauses_and_gaps_are_not_credited(self):
        session = self.start()
        self.event(session, 1, 0)
        self.event(session, 2, 10, after=10)
        self.event(session, 3, 10, state="paused", after=1)
        self.event(session, 4, 10, state="playing", after=300)  # 5 minutes paused: nothing credited.
        self.event(session, 5, 20, after=10)
        self.event(session, 6, 30, after=200)  # Heartbeat gap: not credited.
        session.refresh_from_db()
        self.assertEqual(session.credited_ms, 20_000)
        self.assertEqual(session.gap_count, 1)

    def test_replays_duplicates_and_bad_payloads(self):
        session = self.start()
        self.event(session, 1, 0)
        ok = self.event(session, 2, 10, after=10)
        duplicate = self.event(session, 2, 10, after=2)
        self.assertEqual((ok.accepted, duplicate.code), (True, "duplicate"))
        stale = self.event(session, 1, 0, after=2)
        self.assertEqual((stale.accepted, stale.code, stale.http_status), (False, "out_of_order", 409))
        bad_payloads = [None, [], {"seq": 3}, {"seq": "3", "position_ms": 1, "state": "playing"},
                        {"seq": 3, "position_ms": True, "state": "playing"},
                        {"seq": 3, "position_ms": 1, "state": "watching"},
                        {"seq": 3, "position_ms": -5, "state": "playing"},
                        {"seq": 3, "position_ms": 10**8, "state": "playing"},
                        {"seq": 3, "position_ms": 1, "state": "playing", "completed": True}]
        for payload in bad_payloads:
            self.clock.advance(2)
            with self.subTest(payload=payload):
                self.assertFalse(services.record_event(self.member, session.pk, payload).accepted)
        session.refresh_from_db()
        self.assertEqual((session.credited_ms, session.duplicate_event_count, session.out_of_order_count,
                          session.rejected_event_count), (10_000, 1, 1, len(bad_payloads)))
        self.assertTrue(AuditEvent.objects.filter(action="viewing.evidence.rejected").exists())

    def test_too_frequent_events_are_rejected(self):
        session = self.start()
        self.event(session, 1, 0)
        result = self.event(session, 2, 0, after=0.2)
        self.assertEqual((result.accepted, result.http_status), (False, 429))

    def test_missing_duration_fails_closed(self):
        Video.objects.filter(pk=self.ad_video.pk).update(duration_seconds=None)
        session = self.start()
        self.assertIsNone(session.required_ms)
        session = self.watch(session, 95)
        self.assertEqual((session.outcome, session.reasons), (Out.INSUFFICIENT_EVIDENCE, ["duration_unknown"]))

    def test_revocation_during_playback(self):
        session = self.start()
        self.event(session, 1, 0)
        advertising.pause_campaign(self.owner, self.campaign)
        result = self.event(session, 2, 10, after=10)
        self.assertEqual((result.accepted, result.code), (False, "campaign_not_active"))
        session.refresh_from_db()
        self.assertEqual((session.state, session.outcome), (St.INVALIDATED, Out.REJECTED))
        self.assertEqual(self.event(session, 3, 20, after=10).code, "session_closed")

    def test_video_unpublished_during_playback(self):
        session = self.start()
        self.event(session, 1, 0)
        videos.unpublish(self.manager, self.ad_video)
        self.assertEqual(self.event(session, 2, 10, after=10).code, "video_unavailable")


class ExpiryTests(ViewingTestCase):
    def test_job_expires_and_closes_idempotently(self):
        unstarted = self.start()
        other_member = make_user("m2@x.example")
        self.active_membership(member=other_member)
        idle = self.start(member=other_member)
        self.event(idle, 1, 0, member=other_member)
        self.event(idle, 2, 10, member=other_member, after=10)
        self.clock.advance(400)
        self.assertEqual(services.expire_sessions(), {"expired": 1, "closed": 1})
        self.assertEqual(services.expire_sessions(), {"expired": 0, "closed": 0})
        unstarted.refresh_from_db()
        idle.refresh_from_db()
        self.assertEqual((unstarted.state, unstarted.outcome), (St.EXPIRED, Out.EXPIRED))
        self.assertEqual((idle.state, idle.outcome), (St.FINALIZED, Out.INSUFFICIENT_EVIDENCE))
        retry = self.start()  # Expired sessions do not use up the view.
        self.assertEqual(retry.state, St.CREATED)

    def test_expired_session_rejects_evidence(self):
        session = self.start()
        self.event(session, 1, 0)
        self.clock.advance(4 * 3600)
        self.assertEqual(self.event(session, 2, 10, after=1).code, "session_closed")


@override_settings(VIEWING_MAX_STARTS_PER_HOUR=50)
class ReviewTests(ViewingTestCase):
    def suspicious(self):
        session = self.start()
        self.event(session, 1, 0)
        seq = 1
        for _ in range(3):  # Three bursts of fast playback, then honest viewing.
            seq += 1
            self.event(session, seq, session.last_position_ms / 1000 + 4, after=1)
            session.refresh_from_db()
        position = session.last_position_ms / 1000
        while position < 95:
            seq += 1
            position = min(95, position + 10)
            self.event(session, seq, position, after=10)
        seq += 1
        self.event(session, seq, 95, state="ended", after=1)
        session.refresh_from_db()
        return session

    def test_flagged_sessions_need_review(self):
        session = self.suspicious()
        self.assertEqual((session.outcome, session.review_status, session.qualifying_slot),
                         (Out.SUSPICIOUS_REVIEW, "PENDING", 1))
        self.assertIn("impossible_speed", session.flags)
        self.assertTrue(AuditEvent.objects.filter(action="viewing.fraud.flagged").exists())
        with self.assertRaises(services.Denied):  # Slot is held while under review.
            self.start()
        for actor in (self.manager, self.owner, self.member):
            with self.subTest(actor=actor.email), self.assertRaises(services.Denied):
                services.review_session(actor, session, "confirm", expected_status="PENDING", reason="Looks fine")
        confirmed = services.review_session(self.root, session, "confirm", expected_status="PENDING",
                                            reason="Network jitter")
        self.assertEqual((confirmed.outcome, confirmed.review_status), (Out.VERIFIED_EVIDENCE, "CONFIRMED"))
        services.review_session(self.root, session, "confirm", expected_status="CONFIRMED", reason="Repeat")
        with self.assertRaises(services.StaleDecision):
            services.review_session(self.root, session, "reject", expected_status="PENDING", reason="Too late")
        self.assertEqual(AuditEvent.objects.filter(action="viewing.review.decided").count(), 1)

    def test_rejection_releases_the_view(self):
        session = self.suspicious()
        with self.assertRaises(Exception):
            services.review_session(self.root, session, "reject", expected_status="PENDING", reason="")
        rejected = services.review_session(self.root, session, "reject", expected_status="PENDING",
                                           reason="Automated playback")
        self.assertEqual((rejected.outcome, rejected.qualifying_slot), (Out.REJECTED, None))
        self.assertEqual(self.start().state, St.CREATED)  # The view can be earned honestly again.


class DatabaseAndFirewallTests(ViewingTestCase):
    def test_database_guards(self):
        session = self.start()
        with self.assertRaises(IntegrityError), transaction.atomic():  # No VERIFIED without evidence.
            WatchSession.objects.filter(pk=session.pk).update(state=St.IN_PROGRESS)
            WatchSession.objects.filter(pk=session.pk).update(state=St.FINALIZED, outcome=Out.VERIFIED_EVIDENCE,
                                                              qualifying_slot=1, finalized_at=self.clock.now)
        with self.assertRaises(DatabaseError), transaction.atomic():
            WatchSession.objects.filter(pk=session.pk).update(campaign_revision=99)
        with self.assertRaises(DatabaseError), transaction.atomic():
            WatchSession.objects.filter(pk=session.pk).delete()
        session = self.watch(session, 95)
        with self.assertRaises(DatabaseError), transaction.atomic():  # Closed evidence is frozen.
            WatchSession.objects.filter(pk=session.pk).update(credited_ms=999_999_999)
        with self.assertRaises(DatabaseError), transaction.atomic():
            WatchSession.objects.filter(pk=session.pk).update(state=St.IN_PROGRESS)

    def test_no_financial_side_effects(self):
        from apps.wallets.models import JournalEntry, JournalLine, LedgerAccount

        before = (JournalEntry.objects.count(), JournalLine.objects.count(), LedgerAccount.objects.count())
        self.watch(self.start(), 95)
        self.assertEqual((JournalEntry.objects.count(), JournalLine.objects.count(), LedgerAccount.objects.count()),
                         before)
        self.campaign.refresh_from_db()
        self.assertEqual((self.campaign.funding_status, self.campaign.financially_eligible), ("UNFUNDED", False))

    def test_audit_has_no_playback_secrets(self):
        session = self.start()
        services.playback_for(self.member, session.pk)
        self.watch(session, 95)
        for event in AuditEvent.objects.filter(category="VIEWING"):
            text = str(event.metadata) + event.target_repr
            self.assertNotIn("token", text)
            self.assertNotIn("mediadelivery", text)

    def test_session_ids_are_random(self):
        ids = {self.start().pk}
        for index in range(3):
            member = make_user(f"r{index}@x.example")
            self.active_membership(member=member)
            ids.add(self.start(member=member).pk)
        self.assertEqual(len(ids), 4)
        self.assertTrue(all(i.version == 4 for i in ids))


class ConfigurationTests(ViewingTestCase):
    def test_threshold_checks(self):
        from apps.viewing.checks import check_viewing_thresholds

        self.assertEqual(check_viewing_thresholds(None), [])
        with override_settings(VIEWING_MIN_WATCH_PERCENT=10):
            self.assertEqual([e.id for e in check_viewing_thresholds(None)], ["viewing.E001"])
        with override_settings(VIEWING_MAX_HEARTBEAT_GAP_SECONDS=15, VIEWING_HEARTBEAT_SECONDS=10):
            self.assertEqual([e.id for e in check_viewing_thresholds(None)], ["viewing.E002"])

    def test_required_time(self):
        self.assertEqual(services.required_ms_for(95), 85_500)
        self.assertEqual(services.required_ms_for(10), 10_000)  # Short videos: watch it all.
        self.assertIsNone(services.required_ms_for(3))
        self.assertIsNone(services.required_ms_for(None))
        with override_settings(VIEWING_MIN_WATCH_PERCENT=100):
            self.assertEqual(services.required_ms_for(95), 95_000)

