from datetime import timedelta
from unittest import mock

from django.test import override_settings
from django.utils import timezone

from apps.advertising.tests.helpers import AdvertisingTestCase, User, enable_advertisers, make_user
from apps.viewing import services
from apps.viewing.models import WatchSession


class Clock:
    """Controllable server time (django.utils.timezone.now)."""

    def __init__(self):
        self.now = timezone.now()

    def advance(self, seconds):
        self.now += timedelta(seconds=seconds)
        return self.now


@override_settings(VIEWING_MAX_EVENTS_PER_MINUTE=120)
class ViewingTestCase(AdvertisingTestCase):
    """``member`` has an active membership; ``campaign`` is ACTIVE with a 95 s video."""

    def setUp(self):
        super().setUp()
        self.active_membership()
        self.campaign = self.running_campaign()
        self.clock = Clock()
        patcher = mock.patch("django.utils.timezone.now", side_effect=lambda: self.clock.now)
        patcher.start()
        self.addCleanup(patcher.stop)

    def start(self, member=None, campaign=None):
        return services.start_session(member or self.member, (campaign or self.campaign).pk)

    def event(self, session, seq, position_s, state="playing", member=None, after=None):
        if after is not None:
            self.clock.advance(after)
        return services.record_event(member or self.member, session.pk,
                                     {"seq": seq, "position_ms": int(position_s * 1000), "state": state})

    def watch(self, session, until_s, *, step=10, start_seq=1, speed=1.0, end=True):
        """Honest playback from 0 to ``until_s`` with a heartbeat every ``step`` seconds."""
        seq = start_seq
        self.event(session, seq, 0)
        position = 0
        while position < until_s:
            seq += 1
            position = min(until_s, position + step * speed)
            self.event(session, seq, position, after=step)
        if end:
            seq += 1
            self.event(session, seq, until_s, state="ended", after=1)
        session.refresh_from_db()
        return session


__all__ = ["Clock", "User", "ViewingTestCase", "WatchSession", "enable_advertisers", "make_user"]
