"""Member referral pages (/referrals/). Only the signed-in member's own referrals; referred members
are shown masked (initials only: never a phone number, email address, identity or payout detail)."""

from urllib.parse import quote

from django.contrib import messages
from django.shortcuts import redirect, render
from django.utils.translation import gettext as _
from django.views import View

from apps.accounts.access import VerifiedAccountRequiredMixin

from . import services


class ReferralOverviewView(VerifiedAccountRequiredMixin, View):
    def get(self, request):
        eligibility = services.code_eligibility(request.user)
        code = services.ensure_code(request.user) if eligibility == "ok" else None
        policy = services.peek_policy()
        link = services.share_url(request, code) if code else ""
        share_text = _("Join me on ClipCash, where members watch short sponsored videos. Use my referral code "
                       "%(code)s when you sign up: %(link)s") % {"code": code.code, "link": link} if code else ""
        return render(request, "referrals/overview.html", {
            "nav": "referrals", "eligibility": eligibility, "code": code, "link": link, "policy": policy,
            "share_text": share_text, "whatsapp_url": f"https://wa.me/?text={quote(share_text)}" if code else "",
            "rewards_active": services.rewards_active(),
            "overview": services.member_overview(request.user) if code else None})


class JoinView(View):
    """A shared referral link: remember the code for this visitor's registration, then sign up."""

    def get(self, request, code):
        if request.user.is_authenticated:
            messages.info(request, _("You already have a ClipCash account, so referral links do not apply."))
            return redirect("dashboard")
        services.remember_code(request, code)
        return redirect("accounts:register")
