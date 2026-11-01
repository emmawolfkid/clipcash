from django.conf import settings
from django.core.checks import Error, register

# (setting, minimum, maximum): conservative bounds for viewing thresholds.
RANGES = (
    ("VIEWING_MIN_WATCH_PERCENT", 50, 100),
    ("VIEWING_MIN_WATCH_SECONDS", 5, 600),
    ("VIEWING_HEARTBEAT_SECONDS", 2, 30),
    ("VIEWING_MAX_HEARTBEAT_GAP_SECONDS", 10, 120),
    ("VIEWING_MAX_POSITION_JUMP_SECONDS", 1, 60),
    ("VIEWING_IDLE_SECONDS", 60, 3600),
    ("VIEWING_MAX_CONCURRENT_SESSIONS", 1, 3),
    ("VIEWING_MAX_STARTS_PER_HOUR", 1, 200),
    ("VIEWING_MAX_EVENTS_PER_MINUTE", 6, 120),
)


@register()
def check_viewing_thresholds(app_configs, **kwargs):
    errors = []
    for name, low, high in RANGES:
        value = getattr(settings, name, None)
        if type(value) is not int or not low <= value <= high:
            errors.append(Error(f"{name} must be a whole number between {low} and {high}.", id="viewing.E001"))
    gap = getattr(settings, "VIEWING_MAX_HEARTBEAT_GAP_SECONDS", 0)
    beat = getattr(settings, "VIEWING_HEARTBEAT_SECONDS", 0)
    if type(gap) is int and type(beat) is int and gap < beat * 2:
        errors.append(Error("VIEWING_MAX_HEARTBEAT_GAP_SECONDS must be at least twice VIEWING_HEARTBEAT_SECONDS.",
                            id="viewing.E002"))
    return errors
