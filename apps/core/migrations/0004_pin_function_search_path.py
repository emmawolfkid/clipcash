"""
RG-03: pin the search_path of every ClipCash database function.

The guard and trigger functions created by earlier migrations name their tables
without a schema and had no search_path of their own, so names resolved through
the CALLING session's search_path, in which PostgreSQL searches the session's
temporary schema first. Any login allowed to create temporary tables (PUBLIC
has TEMPORARY by default) could therefore shadow a protected table for its own
session and make a check read the look-alike: an unbalanced journal entry could
commit, or a posting could reach a frozen ledger account
(apps/core/tests/test_database_hardening.py reproduces both).

Each function now runs with search_path = pg_catalog, public, pg_temp:
* pg_catalog first, so built-in functions, types and operators cannot be
  replaced by objects in another schema;
* public next, where every ClipCash table and the btree_gist extension live;
* pg_temp last, named explicitly so that temporary tables are never searched
  before public (and temporary functions are never searched at all).

Function bodies, triggers, constraints and their behaviour are unchanged; only
name resolution is fixed. No function is SECURITY DEFINER, and none becomes one.

Forward-only: there is no reverse operation, because un-pinning would reopen the
hole. Older application code is compatible with the pinned functions, so a code
rollback does not need this migration reversed.

Requires the table OWNER (the migration identity): ALTER FUNCTION is refused to
the least-privileged runtime login. A test keeps FUNCTIONS equal to the functions
that actually exist, so a future migration that adds a function must pin it too.
"""

from django.db import migrations

SEARCH_PATH = "pg_catalog, public, pg_temp"

# Every non-extension function in schema public, as regprocedure signatures.
FUNCTIONS = (
    "advertising_campaign_guard()",
    "advertising_no_delete()",
    "advertising_organization_guard()",
    "audit_auditevent_protect()",
    "ledger_account_guard()",
    "ledger_entry_deferred_check()",
    "ledger_immutable()",
    "ledger_line_apply()",
    "ledger_line_deferred_check()",
    "ledger_verify_entry(uuid)",
    "membership_plan_guard()",
    "membership_plan_version_immutable()",
    "membership_subscription_guard()",
    "payments_allocation_guard()",
    "payments_append_only()",
    "payments_decided_once()",
    "payments_intent_guard()",
    "payments_no_delete()",
    "rewards_budget_guard()",
    "rewards_demo_reward_guard()",
    "rewards_no_delete()",
    "rewards_policy_guard()",
    "support_append_only()",
    "support_ticket_guard()",
    "video_category_guard()",
    "video_guard()",
    "viewing_session_guard()",
    "wallets_block_member_postings()",
    "wallets_discrepancy_guard()",
    "wallets_restriction_guard()",
    "wallets_transactions_locked()",
    "wallets_wallet_guard()",
    "withdrawals_append_only()",
    "withdrawals_beneficiary_guard()",
    "withdrawals_demo_guard()",
    "withdrawals_exception_guard()",
    "withdrawals_locked()",
    "withdrawals_no_delete()",
)

FORWARD = "\n".join(f"ALTER FUNCTION public.{signature} SET search_path = {SEARCH_PATH};" for signature in FUNCTIONS)


class Migration(migrations.Migration):
    # After every migration that creates or replaces one of these functions.
    dependencies = [
        ("core", "0003_jobs_and_rate_limits"),
        ("advertising", "0002_integrity"),
        ("audit", "0014_reports_category"),
        ("memberships", "0003_lifecycle_integrity"),
        ("payments", "0003_account_choices"),
        ("rewards", "0002_integrity"),
        ("support", "0002_integrity"),
        ("videos", "0002_integrity"),
        ("viewing", "0003_revocation"),
        ("wallets", "0005_wallet_integrity"),
        ("withdrawals", "0002_integrity"),
    ]

    operations = [
        migrations.RunSQL(FORWARD, reverse_sql=None),
    ]
