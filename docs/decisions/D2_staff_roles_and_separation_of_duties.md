# D2 — Staff Roles and Separation of Duties (Decision Brief)

**Status:** for owner decision. Read-only analysis; no role was created and no permission assigned.
**Date:** 2026-11-03 · **Baseline:** `main` @ `a87c81b` + uncommitted Phase 14-A.

## 1. The question

Several ClipCash workflows require two (or three) **different people**. Today only one person can perform them,
so these workflows cannot be demonstrated or operated. Which staff structure should be adopted, and what may be
done for a controlled demonstration before a permanent structure exists?

## 2. Existing staff role model (evidence)

| Fact | Evidence |
| --- | --- |
| Three roles only: `SUPER_ADMIN`, `CONTENT_MANAGER`, `MEMBER` | `apps/accounts/roles.py` |
| Role fixes the staff/superuser flags, enforced by the DB check `accounts_user_role_matches_flags` | `apps/accounts/models.py:158-165` |
| Each role has one Django group; only Content Managers get explicit permissions (`videos.manage_videos`, `videos.publish_videos`, `videos.manage_categories`) | `ROLE_PERMISSIONS` |
| Super Administrators are Django superusers: **every** permission | Django semantics |
| Only superusers can change roles, groups, individual permissions or `email_verified`; Group admin is superuser-only | `apps/accounts/admin.py:17`, `GroupAdmin` |
| Role or status changes are written to the ClipCash audit trail; **changes to groups or individual permissions are not** (only Django's own admin change log records them) | `UserAdmin.save_model` → `services.log_access_change` |
| No Finance, Support, Reviewer or Auditor role exists | `roles.py`; `provision_staff_permissions` reports this |
| Local development database: **1 account** (the Super Administrator) | read-only count, 2026-11-03 |

### Custom permissions (all granted to superusers implicitly)

| App | Permissions |
| --- | --- |
| advertising | `review_advertisers`, `review_campaigns`, `manage_advertiser_videos` |
| viewing | `review_viewing` |
| rewards | `manage_reward_policies`, `approve_reward_policies`, `manage_demo_rewards` |
| payments (sandbox) | `view_payments`, `operate_sandbox`, `review_reconciliation`, `approve_refunds` |
| wallets | `view_wallets`, `manage_wallet_restrictions` |
| withdrawals (demo) | `view_withdrawals`, `review_withdrawals`, `operate_payout_sandbox`, `view_beneficiaries` |
| memberships | `view_memberships`, `manage_memberships` |
| core | `view_operations`, `retry_job`, `cancel_job`, `manage_schedules` |
| audit | `view_auditevent` (Django default; gates the audit log) |
| videos | `manage_videos`, `publish_videos`, `manage_categories` |

## 3. Operations that need distinct people

| Operation | Step → permission | Separation rule | Enforced by | Minimum distinct people |
| --- | --- | --- | --- | --- |
| Demo reward policy | draft → `rewards.manage_reward_policies`; approve/reject → `rewards.approve_reward_policies` | approver ≠ drafter | service `rewards/services.py:136` + DB `reward_policy_four_eyes` | 2 |
| Sandbox refund | request and decide → both `payments.approve_refunds` | decider ≠ requester | service `payments/services.py:619` + DB `refund_four_eyes` | 2 |
| Dispute resolution (staff-opened) | `payments.approve_refunds` | resolver ≠ opener | service `payments/services.py:699` + DB `dispute_four_eyes` | 2 |
| Wallet restriction lift | place, request lift, decide → all `wallets.manage_wallet_restrictions` | decider ≠ lift requester | service `wallet_services.py:251-259` + DB `wallet_restriction_four_eyes` | 2 |
| Demo withdrawal (normal) | create/review/approve → `withdrawals.review_withdrawals`; initiate payout → `withdrawals.operate_payout_sandbox` | approver ≠ creator and ≠ member; initiator ≠ approver(s) | service `withdrawals/services.py:474-575` + DB `demo_withdrawal_*` constraints | 2 (A creates and initiates, B approves) |
| Demo withdrawal (≥ TZS 500,000 demo amount) | as above + second approval | second approver ≠ creator and ≠ first approver | same | 3 |
| Advertiser funding verification (real ledger) | ledger posting | different actor from the receipt entry | `wallets/services.py:214-218` | locked — not operable |

Single-person reviews (no four-eyes): advertiser verification, campaign review, viewing review (reviewer must
not be the viewing member), complimentary membership grants (Super Administrator only, Phase 14-A).

**Important:** for refunds, wallet lifts and demo withdrawals, both steps use **the same permission**.
Separation is enforced by requiring a *different person*, not a different permission. Splitting roles helps least
privilege but does not by itself create the second person; **at least two distinct accounts holding the permission
are required.**

### Current state

`python manage.py provision_staff_permissions` (preview, read-only) on the local development database reports
**1 holder** for every permission above. **No two-person workflow can currently be completed.** The sandbox-only
workflows (refunds, disputes, payout simulation) also need sandbox payments enabled, which is not the case on the
documented restricted Render demo and which disables the demo email waiver.

## 4. Options

| Option | What | Code / migration | Least privilege | Audit trail of the assignment | Suitable for |
| --- | --- | --- | --- | --- | --- |
| **A. Second Super Administrator** | Create a second named superuser | None | ✘ (full power) | Account creation is audited (`account.created_by_staff`) | Short controlled demo only |
| **B. Named Content Manager accounts with individual permissions** | Superuser creates Content Manager accounts and adds specific permissions in Django admin | None | ✔ per person | ✘ permission edits not in ClipCash audit (Django admin log only) | Demo and interim operation, with a written assignment record |
| **C. Duty groups** (recommended long-term) | Define groups such as "Reviewers", "Rewards Officers", "Rewards Approvers", "Finance Operators", "Finance Approvers", "Payout Operators", "Wallet Officers", "Auditors", provisioned by the command; staff remain role CONTENT_MANAGER and are placed in groups by a superuser | Code change to roles/provisioning; **no migration** (groups and permissions are data) | ✔ | Needs a small addition: audit group/permission changes | Production |
| D. New user roles | Add enum values (e.g. `FINANCE_OFFICER`) | **Migration required** (role check constraint and choices); wider code impact (role checks, admin rules) | ✔ | Role changes already audited | Only if roles must drive more than permissions |

### Recommended minimum-privilege duty groups (Option C, proposal — not approved)

| Group | Permissions | Must not hold |
| --- | --- | --- |
| Reviewers | `advertising.review_advertisers`, `advertising.review_campaigns`, `viewing.review_viewing` | any payments/wallets/withdrawals permission |
| Rewards Officers | `rewards.manage_reward_policies`, `rewards.manage_demo_rewards` | `rewards.approve_reward_policies` |
| Rewards Approvers | `rewards.approve_reward_policies` | `rewards.manage_reward_policies` |
| Finance Operators (sandbox) | `payments.view_payments`, `payments.operate_sandbox`, `payments.review_reconciliation`, `withdrawals.view_withdrawals`, `withdrawals.operate_payout_sandbox` | — |
| Finance Approvers | `payments.view_payments`, `payments.approve_refunds`, `withdrawals.view_withdrawals`, `withdrawals.review_withdrawals` | `withdrawals.operate_payout_sandbox` |
| Wallet Officers | `wallets.view_wallets`, `wallets.manage_wallet_restrictions` | — |
| Support Agents | `accounts.view_user`, `memberships.view_memberships` (Support module permissions later) | finance permissions |
| Auditors | `audit.view_auditevent`, view-only permissions | any change permission |

Because several workflows separate *people* not permissions, staff each group with **at least two people**
(Finance Approvers: two for refunds and wallet lifts; three distinct staff across Finance Approvers/Operators for
high-risk demo withdrawals).

## 5. Controlled demonstration with two distinct authorised actors

Without any code change (Option B, recommended for the demo):

1. Super Administrator creates two staff accounts, e.g. *Demo Reviewer A* and *Demo Approver B* (role Content
   Manager, ACTIVE, confirmed email), with strong unique passwords set through the existing flow.
2. In Django admin (superuser only), give A `rewards.manage_reward_policies`, `rewards.manage_demo_rewards`,
   `wallets.view_wallets`, `wallets.manage_wallet_restrictions`; give B `rewards.approve_reward_policies`,
   `wallets.view_wallets`, `wallets.manage_wallet_restrictions` (and, only on a sandbox-enabled environment, the
   payments/withdrawals permissions for the refund and demo-withdrawal stories).
3. Record the assignment (who, what, why, until when) in a written log, because permission edits are not in the
   ClipCash audit trail.
4. Run `python manage.py provision_staff_permissions` (preview) and confirm each needed permission shows ≥ 2 holders.
5. Demonstrate: A drafts a reward policy → A's approval attempt is refused (four-eyes) → B approves. A requests a
   wallet-restriction lift → B decides.
6. After the demo, remove the permissions or set the accounts' status to DISABLED.

## 6. Changes that need separate owner approval

* Creating any additional staff or superuser account (Options A/B), and the specific permissions given.
* Defining duty groups in code and extending `provision_staff_permissions` to manage them (Option C).
* Any new user role (Option D) — requires a migration.
* Auditing group and permission changes in the ClipCash audit trail.
* Staff MFA before production (audit CC-012).
* Enabling sandbox payments on a demo environment to show refunds, disputes or payout simulation (conflicts with
  the email-verification waiver; see `docs/acceptance/11_client_demo_readiness.md`).
* The Phase 14-A provisioning correction noted in `docs/implementation/phase_14a_final_acceptance_review.md`
  (refuse to add accounts to a role group that carries extra permissions) before the command is used with `--apply`.
