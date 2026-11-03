"""
Advertiser pages (/advertisers/). Every page resolves the organization through
services.organization_for(), so another advertiser's ID is simply "not found".
All changes are POST, CSRF-protected and go through the services.
"""

from django.contrib import messages
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.paginator import Paginator
from django.db.models import Q
from django.http import Http404
from django.shortcuts import redirect, render
from django.utils.decorators import method_decorator
from django.utils.translation import gettext_lazy as _
from django.views import View

from apps.accounts.access import DEMO_UNCONFIRMED_BLOCKED, _deny_or_redirect, demo_unconfirmed
from apps.core.features import feature_required

from . import services
from .forms import CampaignForm, InviteForm, OrganizationForm
from .models import Campaign

advertisers_feature = method_decorator(feature_required("advertisers"), name="dispatch")


def form_errors(form, error):
    if isinstance(error, ValidationError) and hasattr(error, "error_dict"):
        for field, errors in error.message_dict.items():
            form.add_error(field if field in form.fields else None, errors)
    else:
        form.add_error(None, getattr(error, "messages", [str(error)]))


class AdvertiserBaseView(View):
    """Verified account required; resolves ``organization`` (and the user's team
    membership) or 404s."""

    needs_organization = True

    def dispatch(self, request, *args, **kwargs):
        denied = _deny_or_redirect(request, require_verified_email=True)
        if denied:
            return denied
        if self.needs_organization:
            try:
                self.organization = services.organization_for(request.user, kwargs["organization_id"])
            except services.NotFound:
                raise Http404 from None
            self.membership = services.membership_for(request.user, self.organization)
        try:
            return super().dispatch(request, *args, **kwargs)
        except services.NotFound:
            raise Http404 from None
        except services.NotAuthorized:
            raise PermissionDenied from None

    def context(self, **extra):
        context = {"nav": "advertiser"}
        if self.needs_organization:
            context.update({"organization": self.organization, "membership": self.membership,
                            "is_owner": self.membership.role == self.membership.Role.OWNER})
        context.update(extra)
        return context


@advertisers_feature
class HomeView(AdvertiserBaseView):
    needs_organization = False

    def get(self, request):
        memberships = (request.user.advertiser_memberships.select_related("organization").filter(is_active=True)
                       .order_by("organization__display_name"))
        return render(request, "advertising/home.html", self.context(
            memberships=memberships if services.advertiser_account_reason(request.user) == "ok" else [],
            invitations=services.pending_invitations_for(request.user),
            account_reason=services.advertiser_account_reason(request.user)))


@advertisers_feature
class OnboardView(AdvertiserBaseView):
    needs_organization = False
    template_name = "advertising/onboard.html"

    def get(self, request):
        if demo_unconfirmed(request.user):  # Applications need a confirmed email; explained on the home page.
            messages.error(request, DEMO_UNCONFIRMED_BLOCKED)
            return redirect("advertising:home")
        return render(request, self.template_name, self.context(form=OrganizationForm()))

    def post(self, request):
        form = OrganizationForm(request.POST)
        if form.is_valid():
            try:
                organization = services.create_organization(request.user, **form.values())
                messages.success(request, _("Thank you. Your advertiser application is awaiting review."))
                return redirect("advertising:organization", organization_id=organization.pk)
            except (ValidationError, services.InvalidState) as error:
                form_errors(form, error)
            except services.NotAuthorized:  # Already audited by the service.
                if not demo_unconfirmed(request.user):
                    raise
                messages.error(request, DEMO_UNCONFIRMED_BLOCKED)
                return redirect("advertising:home")
        return render(request, self.template_name, self.context(form=form))


@advertisers_feature
class InvitationResponseView(AdvertiserBaseView):
    needs_organization = False

    def post(self, request, invitation_id, response):
        try:
            if response == "accept":
                organization = services.accept_invitation(request.user, invitation_id)
                messages.success(request, _("You joined %(name)s.") % {"name": organization.display_name})
                return redirect("advertising:organization", organization_id=organization.pk)
            if response == "decline":
                services.decline_invitation(request.user, invitation_id)
                messages.success(request, _("Invitation declined."))
            else:
                raise Http404
        except services.InvalidState as error:
            messages.error(request, str(error))
        return redirect("advertising:home")


@advertisers_feature
class OrganizationView(AdvertiserBaseView):
    def get(self, request, organization_id):
        recent = self.organization.campaigns.select_related("video").order_by("-created_at")[:5]
        return render(request, "advertising/organization.html", self.context(
            tab="overview", report=services.organization_report(self.organization), recent=recent))


@advertisers_feature
class ProfileView(AdvertiserBaseView):
    template_name = "advertising/profile.html"

    def render_form(self, request, form):
        return render(request, self.template_name, self.context(tab="profile", form=form))

    def get(self, request, organization_id):
        return self.render_form(request, OrganizationForm(instance=self.organization))

    def post(self, request, organization_id):
        if "resubmit" in request.POST:
            try:
                services.resubmit_organization(request.user, self.organization)
                messages.success(request, _("Application resubmitted for review."))
            except services.InvalidState as error:
                messages.error(request, str(error))
            return redirect("advertising:profile", organization_id=organization_id)
        form = OrganizationForm(request.POST, instance=self.organization)
        if form.is_valid():
            try:
                services.update_organization(request.user, self.organization, form.values())
                messages.success(request, _("Profile saved."))
                return redirect("advertising:profile", organization_id=organization_id)
            except (ValidationError, services.InvalidState) as error:
                form_errors(form, error)
        return self.render_form(request, form)


@advertisers_feature
class TeamView(AdvertiserBaseView):
    template_name = "advertising/team.html"

    def render_page(self, request, form):
        members = self.organization.memberships.select_related("user").filter(is_active=True).order_by("role")
        invitations = self.organization.invitations.filter(status="PENDING").order_by("-created_at")
        return render(request, self.template_name, self.context(tab="team", members=members,
                                                                invitations=invitations, form=form))

    def get(self, request, organization_id):
        return self.render_page(request, InviteForm())

    def post(self, request, organization_id):
        form = InviteForm(request.POST)
        if form.is_valid():
            try:
                services.invite_member(request.user, self.organization, form.cleaned_data["email"],
                                       form.cleaned_data["role"])
                messages.success(request, _("Invitation created. The person sees it after signing in "
                                            "with that email address."))
                return redirect("advertising:team", organization_id=organization_id)
            except (ValidationError, services.InvalidState) as error:
                form_errors(form, error)
        return self.render_page(request, form)


@advertisers_feature
class TeamChangeView(AdvertiserBaseView):
    def post(self, request, organization_id, change, item_id):
        try:
            if change == "remove":
                services.remove_member(request.user, self.organization, item_id)
                messages.success(request, _("Team member removed."))
            elif change == "revoke":
                services.revoke_invitation(request.user, self.organization, item_id)
                messages.success(request, _("Invitation revoked."))
            else:
                raise Http404
        except services.InvalidState as error:
            messages.error(request, str(error))
        if not services.membership_for(request.user, self.organization):
            return redirect("advertising:home")  # The user left the team.
        return redirect("advertising:team", organization_id=organization_id)


@advertisers_feature
class CampaignListView(AdvertiserBaseView):
    def get(self, request, organization_id):
        qs = self.organization.campaigns.select_related("video").order_by("-created_at")
        query = request.GET.get("q", "").strip()[:100]
        status = request.GET.get("status", "")
        if query:
            qs = qs.filter(Q(name__icontains=query) | Q(description__icontains=query))
        if status in Campaign.Status.values:
            qs = qs.filter(status=status)
        page = Paginator(qs, 20).get_page(request.GET.get("page"))
        params = request.GET.copy()
        params.pop("page", None)
        return render(request, "advertising/campaign_list.html", self.context(
            tab="campaigns", page=page, query=query, status=status, statuses=Campaign.Status.choices,
            page_query=params.urlencode()))


@advertisers_feature
class CampaignCreateView(AdvertiserBaseView):
    template_name = "advertising/campaign_form.html"

    def get(self, request, organization_id):
        return render(request, self.template_name, self.context(
            tab="campaigns", form=CampaignForm(organization=self.organization)))

    def post(self, request, organization_id):
        form = CampaignForm(request.POST, organization=self.organization)
        if form.is_valid():
            values, plans = form.values()
            try:
                campaign = services.create_campaign(request.user, self.organization, target_plans=plans, **values)
                messages.success(request, _("Campaign saved as a draft."))
                return redirect("advertising:campaign", organization_id=organization_id, campaign_id=campaign.pk)
            except (ValidationError, services.InvalidState) as error:
                form_errors(form, error)
        return render(request, self.template_name, self.context(tab="campaigns", form=form))


@advertisers_feature
class CampaignDetailView(AdvertiserBaseView):
    def get(self, request, organization_id, campaign_id):
        campaign = services.campaign_for(request.user, self.organization, campaign_id)
        preview_reason, preview = services.preview_playback(request.user, campaign)
        eligibility = services.campaign_eligibility(campaign)
        return render(request, "advertising/campaign_detail.html", self.context(
            tab="campaigns", campaign=campaign, plans=campaign.target_plans.order_by("name"),
            preview=preview, preview_reason=preview_reason, eligibility=eligibility,
            eligibility_message=services.eligibility_message(eligibility.reason),
            performance=services.campaign_report(campaign)))


@advertisers_feature
class CampaignEditView(AdvertiserBaseView):
    template_name = "advertising/campaign_form.html"

    def get(self, request, organization_id, campaign_id):
        campaign = services.campaign_for(request.user, self.organization, campaign_id)
        if not campaign.is_editable:
            messages.error(request, _("Only draft campaigns can be edited."))
            return redirect("advertising:campaign", organization_id=organization_id, campaign_id=campaign_id)
        form = CampaignForm(instance=campaign, organization=self.organization,
                            initial={"target_plans": campaign.target_plans.all()})
        return render(request, self.template_name, self.context(tab="campaigns", form=form, campaign=campaign))

    def post(self, request, organization_id, campaign_id):
        campaign = services.campaign_for(request.user, self.organization, campaign_id)
        form = CampaignForm(request.POST, instance=services.campaign_for(request.user, self.organization,
                                                                         campaign_id),
                            organization=self.organization)
        try:
            expected = int(request.POST.get("revision", ""))
        except ValueError:
            expected = -1
        if form.is_valid():
            values, plans = form.values()
            try:
                services.update_campaign(request.user, campaign, values, expected_revision=expected,
                                         target_plans=plans)
                messages.success(request, _("Campaign saved."))
                return redirect("advertising:campaign", organization_id=organization_id, campaign_id=campaign_id)
            except (ValidationError, services.InvalidState, services.StaleDecision) as error:
                form_errors(form, error)
        return render(request, self.template_name, self.context(tab="campaigns", form=form, campaign=campaign))


CAMPAIGN_ACTIONS = {
    "submit": (services.submit_campaign, _("Campaign submitted for review.")),
    "withdraw": (services.withdraw_campaign, _("Campaign withdrawn to draft.")),
    "reopen": (services.reopen_campaign, _("Campaign reopened as a draft. It must be reviewed again.")),
    "pause": (services.pause_campaign, _("Campaign paused.")),
    "resume": (services.resume_campaign, _("Campaign resumed.")),
    "cancel": (services.cancel_campaign, _("Campaign cancelled.")),
    "archive": (services.archive_campaign, _("Campaign archived.")),
}


@advertisers_feature
class CampaignActionView(AdvertiserBaseView):
    def post(self, request, organization_id, campaign_id, action):
        if action not in CAMPAIGN_ACTIONS:
            raise Http404
        campaign = services.campaign_for(request.user, self.organization, campaign_id)
        operation, success = CAMPAIGN_ACTIONS[action]
        if action in ("cancel", "archive") and request.POST.get("confirm") != "yes":
            messages.error(request, _("Tick the confirmation box first. This cannot be undone."))
        else:
            try:
                operation(request.user, campaign)
                messages.success(request, success)
            except (ValidationError, services.InvalidState) as error:
                messages.error(request, " ".join(getattr(error, "messages", [str(error)])))
        return redirect("advertising:campaign", organization_id=organization_id, campaign_id=campaign_id)
