# Advertising and campaigns (Phase 05)

This phase covers advertiser organizations, reviewed verification, campaigns
built on Phase 04 videos, server-side eligibility, and demonstration budgets
behind a strict financial firewall. It is gated by the `advertisers` feature
flag (off by default).

**Nothing in this phase is money.** Campaign budgets are demonstration
figures. No campaign can be funded, nothing is written to the ledger, and no
reward, wallet credit or payout exists. Verified viewing (Phase 06) and real
funding (Phase 08) are not implemented.

## Accounts and tenancy

- **No new platform role.** An advertiser is an ordinary verified, ACTIVE member account linked to an `Organization` through `AdvertiserMembership`, with the role OWNER or MANAGER.
- **Organization membership never changes** the account's platform role, staff flags or status.
- **Staff accounts cannot join an organization.** `advertiser_account_reason` returns `staff_account`. As a result, nobody can review their own advertiser or campaign. The services also check this explicitly as `self_review`.
- **Tenant isolation.** Every advertiser page resolves the organization with `services.organization_for(user, id)`, which only searches the user's own organizations. Campaigns are looked up with `campaign_for(user, org, id)`, inside that organization. Any other ID returns **404**, so there's no way to tell that a foreign record exists. Actions use POST and CSRF.
- **Joining** happens only through owner invitations. An invitation is bound to an email address, expires after 7 days, and can be accepted only by a verified account with that email. Owners can remove members, a manager can only leave, and the last owner can't be removed.
- **Documents.** Registration number and TIN are optional text fields. There are no file uploads. These fields are shown only to the organization and to advertiser reviewers, and are never written to the audit trail.

## Verification (organizations)

| From | Allowed to | Who |
| --- | --- | --- |
| PENDING (on creation) | APPROVED, REJECTED (reason required) | `advertising.review_advertisers` |
| APPROVED | SUSPENDED (reason required) | reviewer |
| SUSPENDED | APPROVED (reinstate) | reviewer |
| REJECTED | PENDING (resubmitted) | organization owner |

- Each decision records the reviewer, the time and the reason.
- A decision includes the status the reviewer saw. If the status has since changed, the decision is rejected as **stale**. Submitting the same decision twice does nothing.
- Legal name, country, registration number and TIN are fixed once an organization is approved.
- A user can have only one application awaiting review at a time.

## Videos

- Advertisers don't upload media. A Content Manager uploads the video in the Phase 04 console.
- A user with `advertising.manage_advertiser_videos` then authorizes it for the organization (`AdvertiserVideo`). The authorization can be revoked.
- A campaign may only use a video authorized for its own organization.
- A campaign runs only while its video is published and ready (in an active category), with demo videos only where demo mode is allowed.
- **Previews** (`services.preview_playback`) use the same short-lived signed provider URL as member playback. They never expose Bunny credentials, and they don't send the Phase 04 `playback_authorized` signal.

## Campaign lifecycle

```
DRAFT → SUBMITTED → SCHEDULED → ACTIVE ⇄ PAUSED → COMPLETED → ARCHIVED
          ↘ REJECTED (changes requested) → DRAFT
any not-finished state → CANCELLED → ARCHIVED
SCHEDULED / PAUSED → DRAFT (reopen: needs a new review)
```

- **Editing:** only DRAFT and REJECTED campaigns can be edited. Editing a rejected campaign returns it to DRAFT.
- **Revisions:**
  - Every change increases `revision`.
  - An approval records `approved_revision`.
  - A check constraint requires `approved_revision = revision` in SCHEDULED, ACTIVE, PAUSED and COMPLETED.
  - A trigger freezes the reviewed columns outside DRAFT and REJECTED.
  - So an approved campaign can't be changed without a new review. Reviewer decisions include the revision the reviewer saw (stale protection).
- **Approval** needs:
  - the `advertising.review_campaigns` permission;
  - an approved organization;
  - an authorized, available video;
  - an end date in the future.
  The campaign becomes SCHEDULED, and it becomes ACTIVE immediately if its start time has passed.
- **Schedule job:** `advertising.campaign_schedule` runs every 60 s (Phase 02D). It activates due campaigns, but only while all checks pass, and completes ended ones. It uses SKIP LOCKED and is idempotent.
- **Pausing:** a pause by staff (reason required) can only be lifted by staff.
- **Cancelling and archiving** need an explicit confirmation in the UI.
- **Dates:**
  - Stored timezone-aware (UTC); forms use the site timezone.
  - The end must be after the start and in the future, and a campaign can run at most 366 days.
  - A past start is allowed: the campaign starts as soon as it's approved.
- **Database guards** (migration `advertising/0002_integrity`):
  - no deletes, and status transitions are validated;
  - a campaign can't move to another organization;
  - revisions only increase.

## Targeting and eligibility

`services.campaign_eligibility(campaign, member=None, now=None)` returns an
`EligibilityDecision(eligible, reason, demo_only=True, financially_eligible=False)`.
It denies by default, in this order:

| Reason | Meaning |
| --- | --- |
| `feature_disabled` | The `advertisers` feature flag is off. |
| `unsupported_campaign` | The campaign type isn't supported. |
| `campaign_not_active` | The campaign isn't ACTIVE. |
| `not_started` | The start time hasn't passed. |
| `ended` | The end time has passed. |
| `organization_not_approved` | The advertiser isn't approved. |
| `video_not_authorized` | The video's authorization was revoked. |
| `video_unavailable` | The video isn't published and ready. |
| (membership reasons) | Every Phase 03 `check_access` reason, including the video's required entitlement. |
| `plan_not_targeted` | The member's plan isn't one of the campaign's targeted plans. |
| `ok` | Eligible. |

The only targeting is by membership plan; no plans selected means any active
member. Members have no region or demographic data, and Phase 05 collects
none. `services.eligible_campaigns(member)` lists the active campaigns a
member is eligible for.

## Financial firewall

- **Budget figures** (`demo_budget_amount`, `demo_reward_per_view`) are integers in whole TZS, stored with the project's `AmountField` money convention. Floats and fractions are rejected, never rounded. Both must be positive, and the reward per view can't exceed the budget. They are labelled "demonstration" everywhere.
- **`funding_status`** has the choices UNFUNDED, PENDING_RECONCILIATION, VERIFIED and REJECTED, but the database constraint `adv_campaign_funding_locked` allows **only UNFUNDED**.
  - `services.request_funding_status()` always refuses and records the security event `campaign.funding.refused`.
  - `Campaign.financially_eligible` is False until funding is VERIFIED **and** the locked `real_money_rewards` flag is on.
- **No ledger writes.** The ledger's advertiser accounts (`ADVERTISER_FUNDING_*`, `CAMPAIGN_RESTRICTED_FUNDING`) stay unused; their posting types are locked behind `live_payments` and `real_money_rewards`.
- **Phase 08** must relax the constraint in a migration, and only together with a reference to reconciled advertiser money (a verified payment identity, currency and source). Membership revenue can never fund campaigns: the ledger chart already forbids mixing the two kinds of money.

## Reporting

- `services.organization_report(org)` / `campaign_summary(qs)` produce counts per status and demo-budget totals, all in a single aggregate query.
- Phase 14-C: verified-viewing results come from `viewing.services.campaign_performance(campaigns)` (two aggregate queries): sessions started, verified views, unique verified viewers, awaiting review, not verified. "Verified" uses the same definition as the reward engine (`VERIFIED_EVIDENCE`, finalised, counted slot), so revoked views drop out. Only the organization's own campaigns are counted; no member is identified.
- `services.campaign_report(campaign)` adds the campaign's **simulated** demo-reward figures (recorded, reversed, demo budget used of allocated), always labelled as simulated and never added to any other figure.
- Spend is shown as **"not applicable"**: no money is charged. No metric is invented.
- Owner decision D12 (approved, 5): `DJANGO_ADVERTISING_REPORT_MIN_COUNT` shows non-zero counts below N as "fewer than N" to advertisers (0 = exact). Staff pages always show exact counts.

## Console and interfaces

| Path | Purpose |
| --- | --- |
| `/advertisers/` | Start page: invitations and the user's organizations. |
| `/advertisers/new/` | Onboarding. |
| `/advertisers/<org>/` | Overview and report. |
| `/advertisers/<org>/profile/` | Organization profile. |
| `/advertisers/<org>/team/` | Team and invitations. |
| `/advertisers/<org>/campaigns/` | Campaign list (search, filter, paginate), create, detail with preview, edit, actions. |
| `/manage/advertisers/` | Verification queue (`review_advertisers`), video authorizations (`manage_advertiser_videos`). |
| `/manage/campaigns/` | Review queue and decisions (`review_campaigns`). |

The Django admin has read-only listings. The permissions belong to Super
Administrators; Content Managers get none.

## Audit (category `ADVERTISING`)

| Area | Events |
| --- | --- |
| Organizations | `advertiser.created`, `.updated`, `.resubmitted` |
| Verification | `advertiser.verification.approved`, `.rejected`, `.suspended`, `.reinstated` |
| Team | `advertiser.member.invited`, `.joined`, `.removed`; `advertiser.invitation.declined`, `.revoked` |
| Videos | `advertiser.video.granted`, `.revoked` |
| Campaigns | `campaign.created`, `.updated`, `.submitted`, `.withdrawn`, `.approved`, `.rejected`, `.activated`, `.paused`, `.resumed`, `.completed`, `.cancelled`, `.archived`, `.reopened` |
| Security | `advertiser.action.denied`, `campaign.funding.refused` |

Invitee emails, contact details, TIN and registration numbers are never
recorded.

## Phase 06 contracts

- `services.campaign_eligibility()` and `services.eligible_campaigns()` are the only way to decide whether a member may be shown a campaign.
- `Campaign.max_views_per_member` is stored, but is not enforced until watch sessions exist.
- `Campaign.id`, `Campaign.video_id` and `Campaign.revision` identify exactly what was approved.
- No viewing evidence, impressions or rewards exist yet.

## Limitations

- Targeted plans are a many-to-many relation, so the database trigger can't freeze them. The services protect them, because changes only happen in DRAFT.
- Invitations aren't emailed. The invitee sees them after signing in.
- Organization suspension doesn't change campaign statuses. Eligibility, activation and resuming are blocked while it lasts.
- The table owner can disable the triggers. Production should use the non-owner role setup.
