"""
CR-01: the credit offer is fixed when a sponsored viewing session starts.

quoted_policy / quoted_credits record the approved reward policy and the credits per
verified view the member was shown at start. viewing_session_guard() now treats them as
part of the session's identity: they are set on INSERT and can never change, so a staff
rate change during playback cannot alter what this session earns. Sessions created
before this migration keep NULL (their award follows the policy at finalization, as
before). The search_path stays pinned (RG-03).
"""

from importlib import import_module

import django.db.models.deletion
from django.db import migrations, models

SEARCH_PATH = "pg_catalog, public, pg_temp"
PREVIOUS = import_module("apps.viewing.migrations.0003_revocation").FORWARD
HEADER = "CREATE OR REPLACE FUNCTION viewing_session_guard() RETURNS trigger\nLANGUAGE plpgsql AS $$"
PINNED_HEADER = ("CREATE OR REPLACE FUNCTION viewing_session_guard() RETURNS trigger\n"
                 f"LANGUAGE plpgsql SET search_path = {SEARCH_PATH} AS $$")
IDENTITY = "OR NEW.required_ms IS DISTINCT FROM OLD.required_ms OR NEW.created_at <> OLD.created_at THEN"
QUOTED_IDENTITY = ("OR NEW.required_ms IS DISTINCT FROM OLD.required_ms OR NEW.created_at <> OLD.created_at\n"
                   "       OR NEW.quoted_policy_id IS DISTINCT FROM OLD.quoted_policy_id\n"
                   "       OR NEW.quoted_credits IS DISTINCT FROM OLD.quoted_credits THEN")

assert PREVIOUS.count(HEADER) == 1 and PREVIOUS.count(IDENTITY) == 1
REVERSE = PREVIOUS.replace(HEADER, PINNED_HEADER)
FORWARD = REVERSE.replace(IDENTITY, QUOTED_IDENTITY)


class Migration(migrations.Migration):

    dependencies = [
        ('rewards', '0003_campaign_credit_terms'),
        ('viewing', '0003_revocation'),
        ('core', '0004_pin_function_search_path'),
    ]

    operations = [
        migrations.AddField(
            model_name='watchsession',
            name='quoted_credits',
            field=models.PositiveIntegerField(blank=True, null=True, verbose_name='credits offered at start'),
        ),
        migrations.AddField(
            model_name='watchsession',
            name='quoted_policy',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name='+', to='rewards.rewardpolicy'),
        ),
        migrations.RunSQL(FORWARD, REVERSE),
    ]
