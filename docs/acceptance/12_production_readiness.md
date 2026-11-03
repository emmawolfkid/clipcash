# 12 — Production Readiness

**Verdict: NOT READY for production.** This is assessed separately from the demo. **Real-money activation is
explicitly out of scope**; "production" here means a public, non-financial launch (memberships access,
advertising, viewing, demo-labelled rewards) with real members' personal data.

## 1. Production blockers

| Area | Blocker | Issue |
| --- | --- | --- |
| Data integrity | Database role separation not applied; audit purge flag unrestricted | CC-005 |
| Business flow | No way to activate memberships (paid activation locked; complimentary is policy-gated) — a public launch needs either real payments (separate programme) or an approved free-access model | CC-001 |
| Operations | Worker, SMTP, DB plan/backups, client IP verification | CC-003, CC-004, CC-006 |
| Observability | No error monitoring or alerting | CC-016 |
| Delivery | No CI; restore never rehearsed | CC-017 |
| Video | Bunny configured with CDN token authentication verified | CC-002, CC-025 |
| Product completeness | Support, Notifications, Reports, profile self-service | CC-007, CC-008, CC-009, CC-013 |
| Security posture | Staff MFA; HSTS enablement | CC-012, CC-027 |
| Quality | Browser/E2E tests and accessibility verification | CC-015 |
| Anti-fraud | Accepted-risk statement for demo rewards; hardening plan before any real money | CC-014 |
| Localisation | Swahili offered but untranslated | CC-030 |
| Legal | Terms, privacy notice, brand/terminology review, data-retention policy confirmation (audit retention 365 days) | CC-028 |

## 2. What is already production-grade (evidence)

* Authentication, session and admin privilege controls ([04_security_findings.md](04_security_findings.md) §1).
* Authorization with service-level re-checks and organisation isolation (§2 of the same report).
* CSP without inline code, autoescaping without exceptions, CSRF on all browser forms.
* Durable job system with leases, retries and stale detection; email outbox with expiry.
* Financial locks enforced in PostgreSQL ([05_financial_integrity.md](05_financial_integrity.md)).
* Deployment guide with rollback, troubleshooting and explicit blocker list ([docs/deployment/render.md](../deployment/render.md)).
* 860 passing PostgreSQL tests, including direct-SQL bypass tests for the ledger.

## 3. Out of scope (must stay locked)

Live payments (AzamPay or other), real-money rewards, real withdrawals/payouts, real campaign funding. Each requires a
separately approved architecture, legal/provider approval (Render B7), reviewed migrations replacing the current
locks, and the prerequisites in [05_financial_integrity.md §7](05_financial_integrity.md).
