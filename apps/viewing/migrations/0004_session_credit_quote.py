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

from apps.core.migration_ops import MariaDBRunSQL, PostgreSQLRunSQL, changed

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

# DB-01: on MariaDB, viewing_session_update (from viewing.0003) gets the same two identity
# columns; everything else in it is unchanged. The reverse restores 0003's trigger exactly.
MARIADB_PREVIOUS = import_module("apps.viewing.migrations.0003_revocation")
MARIADB_IDENTITY = changed(*MARIADB_PREVIOUS.SESSION_IDENTITY)
MARIADB_QUOTED_IDENTITY = changed(*MARIADB_PREVIOUS.SESSION_IDENTITY, "quoted_policy_id", "quoted_credits")
(MARIADB_SESSION_UPDATE,) = [s for s in MARIADB_PREVIOUS.MARIADB_FORWARD
                             if s.startswith("CREATE TRIGGER viewing_session_update ")]
assert MARIADB_SESSION_UPDATE.count(MARIADB_IDENTITY) == 1
MARIADB_REVERSE = ["DROP TRIGGER IF EXISTS viewing_session_update", MARIADB_SESSION_UPDATE]
MARIADB_FORWARD = ["DROP TRIGGER IF EXISTS viewing_session_update",
                   MARIADB_SESSION_UPDATE.replace(MARIADB_IDENTITY, MARIADB_QUOTED_IDENTITY)]


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
        PostgreSQLRunSQL(FORWARD, REVERSE),
        MariaDBRunSQL(MARIADB_FORWARD, MARIADB_REVERSE),
    ]
