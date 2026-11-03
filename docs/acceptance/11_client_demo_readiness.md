# 11 — Client Demonstration Readiness

**Verdict: CONDITIONALLY READY** for a staff-guided demonstration on a local or isolated environment;
**NOT READY** for an unattended client demo on the documented Render configuration until CC-001, CC-002 and
CC-003 are resolved (by configuration and the small batch 14-A).

Evidence base: source inspection + 860 passing tests. No workflow was demonstrated in a browser during the audit.

## 1. Workflow-by-workflow assessment

| Workflow | Local (DEBUG, sandbox on, demo video) | Restricted Render demo (as documented) | Prerequisites / demo data |
| --- | --- | --- | --- |
| Registration & login | ✅ demonstrable (console email) | ⚠️ registration **closed** by design; login works for pre-created accounts; email activation needs worker + SMTP (CC-003); proxy IP risk (CC-004) | Admin-created ACTIVE demo accounts; optional `DJANGO_DEMO_ACCOUNTS` waiver |
| Membership selection | ✅ choose plan | ✅ choose plan | Plans created in Django admin |
| Membership activation | ⚠️ CLI `grant_complimentary_membership` with grants flag | ⛔ no shell on free plan; no console action (CC-001) | Grants flag + super admin; batch 14-A |
| Video browsing | ✅ with demo provider | ⛔ without Bunny or approved sample clip (CC-002) | Videos uploaded, published, with duration |
| Advertiser workflows | ✅ onboarding, team, campaigns, staff review | ✅ same (advertiser actions need **verified** email — waiver does not cover them) | Verified advertiser account; staff reviewer with permissions; granted video |
| Sponsored viewing | ✅ if a sample clip is configured (placeholder cannot verify) | ⛔ until CC-001 + CC-002 | Active membership, ACTIVE campaign, approved org, playable video ≥ 5 s |
| Simulated rewards | ✅ after approved policy (two staff) + evaluation | ⚠️ needs worker or staff "evaluate" | RewardPolicy drafted by one staff, approved by another |
| Wallet visibility | ✅ shows TZS 0 with notices | ✅ (verified email required for wallet creation) | — |
| Demo withdrawals | ✅ staff-run with sandbox payouts | ⚠️ creation/review possible; **payout simulation unavailable** while payments disabled | Member with beneficiary; 2–3 distinct staff (creator ≠ approver ≠ initiator) |
| Sandbox payments | ✅ | ⛔ disabled by design (enabling also disables the email waiver) | Decide which story to show |
| Administration | ✅ | ✅ | Staff accounts with explicit permissions (CC-012) |
| Reports | ⚠️ overview counts only | ⚠️ same | — (CC-007) |
| Support | ⛔ not implemented | ⛔ | — (CC-008) |

## 2. Blockers

| Blocker | Type | Resolution path |
| --- | --- | --- |
| CC-001 No in-product membership activation | Code (small) / ops | Batch 14-A, or operator CLI with grants flag |
| CC-002 No playable video | Configuration | Bunny credentials, or approved `VIDEO_DEMO_ALLOWED` + licensed clip |
| CC-003 Worker/SMTP | Hosting cost decision | Paid worker + SMTP, or staff-driven demo with pre-provisioned accounts |
| CC-004 Client IP | Verification | B3 procedure; batch 14-C |
| CC-006 DB expiry | Verification | Confirm plan/date |

## 3. Demo data preparation (no seeding was performed by the audit)

1. One Super Administrator; at least two additional staff with reviewer/finance permissions.
2. Membership plan(s) with entitlements matching the video's `required_entitlement`.
3. Category + at least one published video with known duration (≥ 5 s) and a playable source.
4. Approved advertiser organisation (owner + manager), video granted, campaign approved and ACTIVE, demo budget
   and per-view demo reward set.
5. Reward policy drafted by staff A and approved by staff B.
6. Member account(s): verified (or waived) email, complimentary active membership, wallet opened.
7. Optional: beneficiary + demo withdrawal for the payout story (local/sandbox only).

## 4. Safe-demonstration statements (supported by evidence)

* No real money can move: real-ledger postings, wallet postings, real withdrawals and campaign funding are blocked
  in code **and** by PostgreSQL triggers/constraints ([05_financial_integrity.md](05_financial_integrity.md)).
* Every money-like figure on screen is labelled demonstration/sandbox; no page promises earnings ([06](06_ui_ux_findings.md) §2).
