"""FIN-01: staff-configured earning rules (daily limits and the clearance period). Audited."""

from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils.translation import gettext as _

from apps.audit.services import record_business_event

from .models import MAX_CLEARANCE_HOURS, EarningSettings, PlanEarningLimit
from .services import _require, can_manage_rules, earning_settings


def _optional_positive(raw, field, maximum):
    raw = str(raw if raw is not None else "").strip().replace(",", "")
    if raw == "":
        return None
    if not raw.isdigit() or int(raw) <= 0 or int(raw) > maximum:
        raise ValidationError({field: _("Enter a whole number greater than zero, or leave it empty for no limit.")})
    return int(raw)


def update_settings(actor, *, expected_version, daily_amount_limit, daily_view_limit, clearance_hours):
    _require(can_manage_rules, actor, "rules.update")
    amount = _optional_positive(daily_amount_limit, "daily_amount_limit", 100_000_000)
    views = _optional_positive(daily_view_limit, "daily_view_limit", 1000)
    hours = str(clearance_hours if clearance_hours is not None else "").strip()
    if not hours.isdigit() or int(hours) > MAX_CLEARANCE_HOURS:
        raise ValidationError({"clearance_hours": _("Enter a clearance period from 0 to 720 hours.")})
    hours = int(hours)
    with transaction.atomic():
        earning_settings()
        row = EarningSettings.objects.select_for_update().get(pk=1)
        if str(row.version) != str(expected_version):
            raise ValidationError(_("These settings were changed by someone else. Reload and try again."))
        changes = []
        for field, value in (("daily_amount_limit", amount), ("daily_view_limit", views), ("clearance_hours", hours)):
            if getattr(row, field) != value:
                changes.append(f"{field}: {getattr(row, field)} -> {value}")
                setattr(row, field, value)
        if not changes:
            return row
        row.version += 1
        row.updated_by = actor
        row.save()
        record_business_event("earnings.rules.updated", actor=actor, target=row, metadata={
            "version": row.version, "changes": "; ".join(changes)[:200], "plan": ""})
    return row


def set_plan_limit(actor, plan, *, daily_amount_limit, daily_view_limit):
    """Plan limits apply together with the platform limits; the lower one always wins."""
    _require(can_manage_rules, actor, "rules.plan")
    amount = _optional_positive(daily_amount_limit, "daily_amount_limit", 100_000_000)
    views = _optional_positive(daily_view_limit, "daily_view_limit", 1000)
    with transaction.atomic():
        row = PlanEarningLimit.objects.select_for_update().filter(plan=plan).first()
        before = (row.daily_amount_limit, row.daily_view_limit) if row else (None, None)
        if before == (amount, views):
            return row
        if row is None:
            row = PlanEarningLimit(plan=plan)
        row.daily_amount_limit, row.daily_view_limit, row.updated_by = amount, views, actor
        row.save()
        record_business_event("earnings.rules.updated", actor=actor, target=row, metadata={
            "version": 0, "plan": plan.code, "changes": f"amount: {before[0]} -> {amount}; views: {before[1]} -> {views}"})
    return row
