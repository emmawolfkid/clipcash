# 06 — UI, UX and Accessibility Findings (Phase 13F)

**Method:** source-based review of 108 templates, `static/css/clipcash.css` (402 lines) and three JavaScript files
(`member-nav.js`, `video-upload.js`, `video-watch.js`). **No browser automation was available**, so rendered
layout, colour contrast, keyboard focus order, screen-reader output and mobile behaviour are **unverified**.
Pages are exercised by Django test-client tests (HTML responses), not visually.

## 1. Structural checks performed

| Check | Result |
| --- | --- |
| Broken links (`{% url %}` names) | **None.** All template URL names resolve against the 218 named routes (static script run during the audit) |
| Missing templates | **None.** Every template referenced from code/includes exists; no orphan templates |
| Inline scripts / styles / event handlers (would be blocked by CSP) | **None found** |
| Unsafe output (`|safe`, `mark_safe`) | **None found** |
| `<html lang>` and viewport | Present in [templates/base.html](../../templates/base.html) |
| Skip link | Present ("Skip to content" → `#content`) |
| Landmarks | `header`, `nav` with `aria-label`, `main#content`, `footer` |
| Current-page indication | `aria-current="page"` in member navigation |
| Forms | Shared component renders `field.as_field_group` (label + help + errors) and non-field errors with `role="alert"` ([templates/components/form.html](../../templates/components/form.html)) |
| Live regions | 9 `role=status/alert` / `aria-live` usages, including the watch-progress status |
| Tables | 37 tables, 37 `scope`/`caption` usages (pattern count; per-table correctness not verified) |
| Images | No `<img>` without `alt` |
| Responsive CSS | Breakpoints at 30 rem, 40 rem, 52 rem; dark-mode support; collapsible member menu (`member-nav.js`) |
| Focus styles | 2 `:focus` rules in CSS (adequacy unverified) |
| Empty states | `{% empty %}` / `.empty-state` used in 35 templates |

## 2. Financial representation (critical check)

**No interface was found that presents simulated rewards as real earnings or suggests real payouts are enabled.**
A pattern scan for "earn/earnings/cash out/get paid/payout/real money" found only negated or demo-labelled uses,
for example:

* `components/not_a_reward_notice.html` — "Watching sponsored videos does not earn money yet. No balance, payment or reward is created."
* `memberships/plan_detail.html` — "A membership … is not an investment and does not promise any earnings."
* `public/home.html` — "…a promise of earnings."
* Demo notices: `demo_reward_notice`, `demo_budget_notice`, `demo_withdrawal_notice`, `sandbox_notice`, `wallet_notice`.
* Campaign form help: "Demonstration figure only. It is not money and is never charged or paid out." ([apps/advertising/forms.py:56](../../apps/advertising/forms.py#L56))
* Restricted-demo banner: "Real payments and payouts are disabled."

Residual consideration: the **brand name "ClipCash"** and "Payout destinations" wording invite an earnings
expectation; a legal/marketing review is recommended before any public launch (CC-028, P3).

## 3. Area-by-area review

| Area | Templates | Observations | Status |
| --- | --- | --- | --- |
| Public landing | `public/home.html` | Restricted-demo aware; registration link hidden when closed | ✅ |
| Registration / login / verification | `accounts/*` (22) | Uniform responses; clear invalid-link page; resend flow | ✅ |
| Member dashboard | `members/dashboard.html`, `layout.html`, `activity.html` | Summary cards per module; navigation to every member module | ✅ |
| Membership selection | `memberships/*` | Selection works; **after choosing, the member reaches a PENDING state with no way to complete it in product** (CC-001). Message clarity in that state should be reviewed in a browser | ⚠️ |
| Video catalogue & playback | `videos/*`, `components/video_card.html`, `preview_player.html` | Placeholder player shows an explicit message when no video file is configured | ⚠️ (CC-002) |
| Sponsored viewing | `viewing/*` | Progress bar + polite live status; JS status strings are **hard-coded English** in `video-watch.js` (not translatable) | ⚠️ |
| Advertiser dashboard / campaigns | `advertising/*` | Team, profile, campaigns, funding; report tiles show "not available yet" for verified views/viewers/spend (CC-010) | ⚠️ |
| Administration | `administration/*` (30+) | Permission-aware nav; queues; decision forms with confirmation for irreversible cancel | ✅ |
| Wallet & rewards | `wallets/*`, `rewards/history.html` | Clear zero-money notices | ✅ |
| Demo withdrawals | `withdrawals/*` | Demo notices; destinations; eligibility reasons | ✅ |
| Notifications | — | **No notification centre / inbox UI** (CC-009) | ⛔ missing |
| Support | — | **No help/contact/ticket pages**; only optional footer email/phone from site configuration (CC-008) | ⛔ missing |
| Reports | — | **No reports pages** (CC-007) | ⛔ missing |

## 4. Findings

| ID | Sev | Finding | Evidence | Confidence |
| --- | --- | --- | --- | --- |
| CC-030 | P3 | **Swahili is selectable but untranslated.** `LANGUAGES` offers `sw` and the header shows a language switcher, but `locale/` contains only `.gitkeep`; 1,274 translatable template tags would render in English | [config/settings.py:404-409](../../config/settings.py#L404-L409), `locale/` | Confirmed |
| CC-031 | P3 | Watch-page JavaScript status messages are English literals, bypassing i18n | [static/js/video-watch.js:41-50](../../static/js/video-watch.js#L41-L50) | Confirmed |
| CC-008 | P2 | "Please contact support" shown to advertisers when a campaign is paused by staff, but no support channel exists | [apps/advertising/services.py:700-702](../../apps/advertising/services.py#L700-L702) | Confirmed |
| CC-001 | P1 | Member can choose a plan but cannot complete activation in the product | see 03 | Confirmed |
| CC-010 | P2 | Advertiser report shows placeholders instead of available verified-view data | `templates/components/campaign_report.html` | Confirmed |
| CC-015 | P2 | No browser-level verification of layout, keyboard use, contrast, or the Bunny player event bridge | — | Verification gap |
| CC-028 | P3 | Brand/terminology review ("ClipCash", "payout") | — | Advisory |

## 5. Not verified (requires a browser session)

Colour contrast ratios (including dark mode), focus visibility and order, behaviour at 320–400 px widths, the
collapsible member menu, video player controls, the Bunny iframe `player.js` postMessage events, loading/progress
feedback timing, and screen-reader announcements. These are included in
[14_manual_acceptance_checklist.md](14_manual_acceptance_checklist.md).
