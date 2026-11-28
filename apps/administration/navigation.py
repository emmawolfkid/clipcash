"""
OPS-01: the Operations Console navigation, defined once.

Every console page belongs to exactly one section. The sidebar shows groups (Overview, Management,
Finance, Services and reports, System) of sections; a section opens to its pages. Only pages the staff
member may open are listed (``console_links`` mirrors each page's own permission check, and every page
still enforces it), and a section without any visible page is hidden.

``ITEMS`` is also the reviewed route -> menu mapping: ``ROUTE_TO_ITEM`` names the entry highlighted on
each console route (detail and action pages highlight their list), so ``test_navigation`` fails until a
new console route is placed somewhere. Sandbox and demonstration tools are listed only while
``SIMULATION_TOOLS_VISIBLE`` is on (isolated development and automated tests); their pages and records
are kept.
"""

from dataclasses import dataclass, field

from django.conf import settings
from django.urls import reverse
from django.utils import translation
from django.utils.translation import gettext_lazy as _


@dataclass(frozen=True)
class Item:
    key: str          # The ``nav`` value pages pass (highlighting).
    target: str       # URL name.
    label: object
    can: str          # Key in console_links(user).
    external: bool = False  # Django admin and account pages: never "current" in the console.
    keywords: tuple = ()


@dataclass(frozen=True)
class Section:
    key: str
    label: object
    icon: str
    group: str
    items: tuple = field(default_factory=tuple)


GROUPS = (
    ("overview", _("Overview")),
    ("management", _("Management")),
    ("finance", _("Finance")),
    ("services", _("Services and reports")),
    ("system", _("System")),
    ("simulation", _("Testing tools")),
)

SECTIONS = (
    Section("overview", _("Overview"), "dashboard", "overview", (
        Item("overview", "administration:overview", _("Overview"), "staff", keywords=("dashboard", "home")),
    )),
    Section("members", _("Members"), "users", "management", (
        Item("members", "administration:members", _("Members"), "members", keywords=("search", "account", "user")),
    )),
    Section("membership", _("Membership"), "membership", "management", (
        Item("plans", "administration:plans", _("Membership plans"), "plans", keywords=("packages", "price")),
    )),
    Section("videos", _("Videos"), "videos", "management", (
        Item("videos", "administration:videos", _("All videos"), "videos", keywords=("upload", "publish")),
        Item("video_new", "administration:video_create", _("Upload video"), "video_manage", keywords=("new", "add")),
        Item("video_storage", "administration:video_storage", _("Video storage"), "videos",
             keywords=("disk", "space", "files")),
        Item("categories", "administration:categories", _("Categories"), "categories"),
        Item("viewing", "administration:viewing", _("Viewing review"), "viewing", keywords=("fraud", "sessions")),
    )),
    Section("advertising", _("Advertising"), "advertiser", "management", (
        Item("campaigns", "administration:campaigns", _("Campaigns"), "campaigns", keywords=("approve",)),
        Item("campaign_new", "administration:campaign_create", _("New campaign"), "campaign_manage"),
        Item("advertisers", "administration:advertisers", _("Advertisers"), "advertisers", keywords=("companies",)),
    )),
    Section("referrals", _("Referrals"), "share", "management", (
        Item("referrals", "administration:referrals", _("Referrals"), "referrals", keywords=("invite", "review")),
        Item("referral_programme", "administration:referral_programme", _("Referral rules"), "referrals"),
        Item("referral_funding", "administration:referral_funding", _("Referral budget"), "referrals"),
    )),
    Section("payments", _("Payments"), "coins", "finance", (
        Item("membership_payments", "administration:membership_payments", _("Verify payments"),
             "membership_payments", keywords=("airtel", "lipa", "pending", "confirm")),
        Item("payment_instructions", "administration:payment_instructions", _("Payment methods"),
             "payment_instructions", keywords=("number", "receiving")),
    )),
    Section("rewards", _("Rewards and funding"), "gift", "finance", (
        Item("video_rewards", "administration:video_rewards", _("Video rewards"), "video_rewards"),
        Item("reward_policies", "administration:reward_policies", _("Reward rates"), "rewards",
             keywords=("amount", "policy", "approve")),
        Item("earning_rules", "administration:earning_rules", _("Earning limits"), "earning_rules"),
        Item("campaign_funding", "administration:campaign_funding", _("Campaign funding"), "campaign_funding",
             keywords=("deposit", "budget")),
    )),
    Section("wallets", _("Wallets"), "wallet", "finance", (
        Item("wallets", "administration:wallets", _("Wallets"), "wallets", keywords=("balance", "restriction")),
        Item("wallet_reconciliation", "administration:wallet_reconciliation", _("Wallet checks"), "wallets",
             keywords=("reconciliation",)),
    )),
    Section("withdrawals", _("Withdrawals"), "withdrawals", "finance", (
        Item("withdrawal_requests", "administration:withdrawal_requests", _("Withdrawal requests"),
             "withdrawal_requests", keywords=("payout", "send money", "approve", "verify")),
        Item("withdrawal_identities", "administration:withdrawal_identities", _("Identity checks"),
             "withdrawal_identities"),
        Item("beneficiaries", "administration:beneficiaries", _("Payout destinations"), "withdrawals"),
        Item("withdrawal_settings", "administration:withdrawal_settings", _("Withdrawal settings"),
             "withdrawal_settings", keywords=("fee", "limits")),
    )),
    Section("reports", _("Reports"), "chart", "services", (
        Item("reports", "administration:reports", _("Reports"), "reports", keywords=("export", "csv", "finance")),
        Item("analytics", "administration:analytics", _("Analytics"), "analytics"),
    )),
    Section("support", _("Customer support"), "support", "services", (
        Item("support", "administration:support", _("Support requests"), "support", keywords=("tickets", "help")),
    )),
    Section("notifications", _("Notifications"), "notifications", "services", (
        Item("notifications", "notifications:inbox", _("My notifications"), "staff", external=True),
    )),
    Section("staff", _("Staff and permissions"), "staff", "system", (
        Item("staff_accounts", "admin:accounts_user_changelist", _("Staff and user accounts"), "members",
             external=True, keywords=("roles", "permissions")),
        Item("staff_groups", "admin:auth_group_changelist", _("Roles and groups"), "admin_site", external=True),
    )),
    Section("settings", _("System settings"), "settings", "system", (
        Item("settings_hub", "administration:settings", _("All settings"), "staff",
             keywords=("configuration", "company", "registration")),
        Item("site_settings", "admin:core_siteconfiguration_changelist", _("Platform settings"), "settings",
             external=True, keywords=("company", "features", "registration")),
    )),
    Section("system", _("System administration"), "server", "system", (
        Item("health", "administration:health", _("System health"), "staff",
             keywords=("jobs", "worker", "cron", "integrity")),
        Item("security", "administration:security", _("Security"), "audit"),
        Item("audit_logs", "admin:audit_auditevent_changelist", _("Audit logs"), "audit", external=True),
        Item("django_admin", "admin:index", _("Django administration"), "admin_site", external=True),
    )),
    Section("simulation", _("Testing tools"), "flag", "simulation", (
        Item("rewards", "administration:demo_rewards", _("Demo rewards"), "rewards"),
        Item("payments", "administration:payments", _("Payments (sandbox)"), "payments"),
        Item("withdrawals", "administration:withdrawals", _("Withdrawals (demo)"), "withdrawals"),
        Item("withdrawal_exceptions", "administration:withdrawal_exceptions", _("Demo payout exceptions"),
             "withdrawals"),
    )),
)

ITEMS = {item.key: item for section in SECTIONS for item in section.items}
SECTION_OF = {item.key: section for section in SECTIONS for item in section.items}

# Console route -> the menu entry highlighted on it (None: not a page of its own).
ROUTE_TO_ITEM = {
    "overview": "overview", "health": "health", "settings": "settings_hub", "video_storage": "video_storage",
    "members": "members", "member": "members", "member_complimentary": "members", "member_membership_action": None,
    "plans": "plans", "plan_create": "plans", "plan": "plans",
    "payment_instructions": "payment_instructions", "payment_method_new": "payment_instructions",
    "payment_method_edit": "payment_instructions", "payment_method_retire": "payment_instructions",
    "payment_method_reactivate": "payment_instructions",
    "membership_payments": "membership_payments", "membership_payment": "membership_payments",
    "videos": "videos", "video_create": "video_new", "video_detail": "videos", "video_upload": None,
    "video_action": None, "video_local_upload": None,
    "categories": "categories", "category_edit": "categories",
    "viewing": "viewing", "viewing_review": "viewing",
    "advertisers": "advertisers", "advertiser_review": "advertisers", "advertiser_create": "advertisers",
    "campaigns": "campaigns", "campaign_review": "campaigns", "campaign_credits": "rewards",
    "campaign_create": "campaign_new", "campaign_edit": "campaigns",
    "demo_rewards": "rewards", "reward_policies": "reward_policies", "reward_policy": "reward_policies",
    "video_rewards": "video_rewards", "video_reward": "video_rewards", "campaign_funding": "campaign_funding",
    "campaign_deposit": "campaign_funding", "earning_rules": "earning_rules",
    "withdrawal_requests": "withdrawal_requests", "withdrawal_request": "withdrawal_requests",
    "withdrawal_settings": "withdrawal_settings", "withdrawal_identities": "withdrawal_identities",
    "referrals": "referrals", "referral_programme": "referral_programme", "referral_funding": "referral_funding",
    "referral": "referrals",
    "payments": "payments", "payment_exceptions": "payments", "payment_funding": "payments", "payment": "payments",
    "wallets": "wallets", "wallet_reconciliation": "wallet_reconciliation", "wallet": "wallets",
    "withdrawals": "withdrawals", "withdrawal_exceptions": "withdrawal_exceptions", "beneficiaries": "beneficiaries",
    "withdrawal": "withdrawals",
    "support": "support", "support_ticket": "support",
    "analytics": "analytics", "reports": "reports", "report": "reports", "report_export": None,
    "security": "security",
}


def simulation_visible():
    return bool(getattr(settings, "SIMULATION_TOOLS_VISIBLE", False))


def _both_languages(label):
    words = []
    for language in ("en", "sw"):
        with translation.override(language):
            words.append(str(label))
    return words


def current_item(request, nav):
    """The highlighted entry: the route's mapping first, then the page's own ``nav`` value."""
    match = getattr(request, "resolver_match", None)
    if match is not None and match.namespace == "administration" and ROUTE_TO_ITEM.get(match.url_name):
        return ROUTE_TO_ITEM[match.url_name]
    return nav if nav in ITEMS else ""


def build(user, can, active=""):
    """Groups -> sections -> visible items for this staff member (one query-free pass)."""
    show_simulation = simulation_visible()
    groups = []
    active_section = SECTION_OF[active].key if active in SECTION_OF else ""
    for group_key, group_label in GROUPS:
        if group_key == "simulation" and not show_simulation:
            continue
        sections = []
        for section in SECTIONS:
            if section.group != group_key:
                continue
            items = []
            for item in section.items:
                if not can.get(item.can):
                    continue
                items.append({
                    "key": item.key, "label": item.label, "url": reverse(item.target), "external": item.external,
                    "current": item.key == active and not item.external,
                    "search": " ".join(_both_languages(item.label) + _both_languages(section.label)
                                       + list(item.keywords)).lower(),
                })
            if items:
                sections.append({"key": section.key, "label": section.label, "icon": section.icon, "items": items,
                                 "url": items[0]["url"], "open": section.key == active_section,
                                 "current": section.key == active_section})
        if sections:
            groups.append({"key": group_key, "label": group_label, "sections": sections})
    return groups


def mapping_rows(can=None):
    """(route, URL pattern, section, entry, permission key) for the navigation mapping report."""
    from apps.administration.urls import urlpatterns

    rows = []
    for pattern in urlpatterns:
        key = ROUTE_TO_ITEM.get(pattern.name)
        item = ITEMS.get(key) if key else None
        section = SECTION_OF.get(key) if key else None
        rows.append((pattern.name, "/manage/" + str(pattern.pattern), section.label if section else "",
                     item.label if item else "", item.can if item else ""))
    return rows
