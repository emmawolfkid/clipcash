"""
CR-01 migration regression: awards made before rewards.0003 keep counting.

A demo award is recorded, the schema is rolled back to before rewards.0003 (no credit
terms, legacy budget guard), then migrated forward again, so 0003 runs over a database
that already holds the award and its consumption. The award and the consumption must be
unchanged, the seeded credit budget must equal the approved campaign budget, and the
prior consumption must reduce what can be awarded next.
"""

from unittest import skipUnless

from django.db import connection
from django.db.migrations.executor import MigrationExecutor

from apps.rewards import services
from apps.rewards.models import CampaignCreditTerms, DemoReward

from .test_concurrency import RewardConcurrencyTests, verified_session
from .test_credits import quoted_verified_session

BEFORE_CR01 = [("rewards", "0002_integrity"), ("viewing", "0003_revocation")]


def migrate(targets=None):
    executor = MigrationExecutor(connection)
    executor.loader.build_graph()
    executor.migrate(targets or executor.loader.graph.leaf_nodes())


def table_exists(name):
    with connection.cursor() as cursor:
        return name in connection.introspection.table_names(cursor)


@skipUnless(connection.vendor == "postgresql", "Migration guards are verified on PostgreSQL.")
class CreditTermsMigrationTests(RewardConcurrencyTests):
    """Setup: approved campaign budget 300, approved policy 100 credits per view."""

    test_parallel_evaluation_records_one_decision = None
    test_concurrent_budget_exhaustion = None
    test_parallel_jobs_and_reversals_release_once = None

    def test_award_made_before_0003_is_preserved_and_counts(self):
        award = services.evaluate_session(verified_session(self.member("before@x.example"), self.campaign).pk)
        self.assertEqual((award.status, award.amount), ("DEMO_QUALIFIED", 100))
        self.assertFalse(CampaignCreditTerms.objects.exists())

        self.addCleanup(migrate)  # Always end at the latest schema.
        migrate(BEFORE_CR01)
        self.assertFalse(table_exists("rewards_campaigncreditterms"))
        with connection.cursor() as cursor:
            cursor.execute("SELECT status, amount FROM rewards_demoreward WHERE id = %s", [award.pk])
            self.assertEqual(cursor.fetchone(), ("DEMO_QUALIFIED", 100))
            cursor.execute("SELECT demo_consumed FROM rewards_campaignrewardbudget WHERE campaign_id = %s",
                           [self.campaign.pk])
            self.assertEqual(cursor.fetchone(), (100,))

        migrate()  # rewards.0003 (seed), viewing.0004, rewards.0004 run over the existing award.
        award.refresh_from_db()
        self.assertEqual((award.status, award.amount, award.policy_version), ("DEMO_QUALIFIED", 100, 1))
        terms = CampaignCreditTerms.objects.get(campaign=self.campaign)
        self.assertEqual(terms.credit_budget, 300)  # Seeded from the approved campaign budget.
        self.assertEqual(services.budget_summary(self.campaign),
                         {"demo_allocated": 300, "demo_consumed": 100, "demo_remaining": 200})

        policy, credits = services.current_offer(self.campaign)
        outcomes = [services.evaluate_session(quoted_verified_session(
            self.member(f"after{i}@x.example"), self.campaign, policy, credits).pk) for i in range(3)]
        self.assertEqual([(o.status, o.reasons) for o in outcomes],
                         [("DEMO_QUALIFIED", ["eligible"]), ("DEMO_QUALIFIED", ["eligible"]),
                          ("DEMO_REJECTED", ["budget_exhausted"])])
        self.assertEqual(services.budget_summary(self.campaign)["demo_consumed"], 300)
        self.assertEqual(DemoReward.objects.filter(status="DEMO_QUALIFIED").count(), 3)


del RewardConcurrencyTests
