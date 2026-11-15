"""
CR-01: the effective credit terms of a campaign, in one place (no service imports, so
advertising and viewing can use it without import cycles).

A campaign with staff CampaignCreditTerms uses them. A campaign without (created after
CR-01 and not yet configured by staff) uses what its approved revision says: the demo
budget and the target plans. That is also what the CR-01 migration copied into the terms
of every existing campaign, so the two never disagree until staff change something.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class EffectiveTerms:
    credit_budget: int
    plan_ids: frozenset  # Empty: every plan that includes the video.
    configured: bool  # True when staff terms exist.


def _terms_of(campaign):
    from django.core.exceptions import ObjectDoesNotExist

    try:
        return campaign.credit_terms
    except ObjectDoesNotExist:
        return None


def effective_terms(campaign):
    terms = _terms_of(campaign)
    if terms is not None:
        return EffectiveTerms(terms.credit_budget, frozenset(plan.pk for plan in terms.eligible_plans.all()), True)
    return EffectiveTerms(campaign.demo_budget_amount, frozenset(plan.pk for plan in campaign.target_plans.all()),
                          False)


def eligible_plan_ids(campaign):
    return effective_terms(campaign).plan_ids


def plan_allowed(campaign, plan_id):
    plan_ids = eligible_plan_ids(campaign)
    return not plan_ids or plan_id in plan_ids


# Prefetch these on campaign querysets that check plans for many campaigns.
PREFETCH = ("target_plans", "credit_terms__eligible_plans")
