"""
CR-01: staff-owned credit terms per sponsored campaign (non-cash credits).

* CampaignCreditTerms: total credit budget and eligible membership plans, set by
  authorized staff and audited. The credits per verified view stay in RewardPolicy
  (versioned, four-eyes approval).
* Existing campaigns get terms equal to what they already had: credit budget = the
  approved demo budget, eligible plans = the approved target plans. Behaviour is
  unchanged until staff edit the terms. Past demo rewards are never rewritten.
* rewards_budget_guard() now caps consumption at the staff credit budget when terms
  exist (else at the campaign's approved demo budget, as before). The search_path stays
  pinned (RG-03).
"""

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models

SEARCH_PATH = "pg_catalog, public, pg_temp"

BUDGET_GUARD = r"""
CREATE OR REPLACE FUNCTION rewards_budget_guard() RETURNS trigger
LANGUAGE plpgsql SET search_path = {path} AS $$
DECLARE
    allocated bigint;
BEGIN
    IF NEW.demo_consumed > COALESCE(OLD.demo_consumed, 0) OR TG_OP = 'INSERT' THEN
        {lookup}
        IF NEW.demo_consumed > allocated THEN
            RAISE EXCEPTION 'simulated consumption exceeds the campaign demo budget'
                USING ERRCODE = 'check_violation';
        END IF;
    END IF;
    RETURN NEW;
END;
$$;
"""

FORWARD = BUDGET_GUARD.format(path=SEARCH_PATH, lookup=(
    "SELECT credit_budget INTO allocated FROM rewards_campaigncreditterms WHERE campaign_id = NEW.campaign_id;\n"
    "        IF NOT FOUND THEN\n"
    "            SELECT demo_budget_amount INTO allocated FROM advertising_campaign WHERE id = NEW.campaign_id;\n"
    "        END IF;"))
REVERSE = BUDGET_GUARD.format(path=SEARCH_PATH, lookup=(
    "SELECT demo_budget_amount INTO allocated FROM advertising_campaign WHERE id = NEW.campaign_id;"))


def seed_terms(apps, schema_editor):
    Campaign = apps.get_model("advertising", "Campaign")
    Terms = apps.get_model("rewards", "CampaignCreditTerms")
    for campaign in Campaign.objects.all().iterator():
        terms, created = Terms.objects.get_or_create(campaign=campaign,
                                                     defaults={"credit_budget": campaign.demo_budget_amount})
        if created:
            terms.eligible_plans.set(campaign.target_plans.all())


class Migration(migrations.Migration):

    dependencies = [
        ('advertising', '0002_integrity'),
        ('memberships', '0003_lifecycle_integrity'),
        ('rewards', '0002_integrity'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name='CampaignCreditTerms',
            fields=[
                ('campaign', models.OneToOneField(on_delete=django.db.models.deletion.PROTECT, primary_key=True, related_name='credit_terms', serialize=False, to='advertising.campaign')),
                ('credit_budget', models.PositiveIntegerField(verbose_name='total credit budget')),
                ('version', models.PositiveIntegerField(default=1, verbose_name='version')),
                ('updated_at', models.DateTimeField(auto_now=True, verbose_name='updated')),
                ('eligible_plans', models.ManyToManyField(blank=True, related_name='+', to='memberships.membershipplan', verbose_name='eligible membership plans')),
                ('updated_by', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name='+', to=settings.AUTH_USER_MODEL)),
            ],
            options={
                'verbose_name': 'campaign credit terms',
                'verbose_name_plural': 'campaign credit terms',
                'constraints': [models.CheckConstraint(condition=models.Q(('credit_budget__gte', 0)), name='credit_terms_budget_valid')],
            },
        ),
        migrations.RunPython(seed_terms, migrations.RunPython.noop),
        migrations.RunSQL(FORWARD, REVERSE),
    ]
