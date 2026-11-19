# UI-09 — branding, Kiswahili and action feedback

## Baseline and scope

Implemented on the user-approved `main` baseline `6952816`, starting with a clean
working tree. The eight commits following `b3b6d17` are preserved. No stage,
commit, push, reset, branch switch or deployment was performed. NetPoa files and
migrations are untouched.

The architecture was inspected before implementation: shared base/public/member/
authentication layouts; the existing favicon/play mark, CSS tokens and modular
stylesheets; the single Django messages include and existing shared JavaScript;
the persistent inbox; LocaleMiddleware, set_language and catalogue-gated language
selector; current phone-first registration, manual Airtel verification and
session-quoted non-cash reward credits.

The implementation follows that architecture: reuse the existing brand asset,
add one shared wordmark include, supply standard Django PO/MO catalogues, retain
the shared language form and enhance the existing Django messages progressively.

## Exact reviewable files

Modified:

- `apps/core/templatetags/money.py`
- `apps/core/tests/test_design_system.py`
- `apps/core/tests/test_deployment.py`
- `apps/core/tests/test_member_viewing_ui.py`
- `apps/core/tests/test_public_auth_ui.py`
- `apps/rewards/services.py`
- `config/settings.py`
- `templates/accounts/layout.html`
- `templates/base.html`
- `templates/components/language_form.html`
- `templates/components/messages.html`
- `templates/components/stylesheets.html`
- `templates/public/_footer.html`

Added:

- `apps/core/templatetags/feedback_tags.py`
- `apps/core/tests/test_ui09.py`
- `docs/implementation/ui_09_branding_i18n_feedback.md`
- `locale/sw/LC_MESSAGES/django.po`
- `locale/sw/LC_MESSAGES/django.mo`
- `scripts/run_ui09_regressions.py`
- `scripts/verify_i18n.py`
- `scripts/verify_ui09_toasts.mjs`
- `static/css/components/toasts.css`
- `static/css/components/wordmark.css`
- `static/js/toasts.js`
- `templates/components/wordmark.html`

Ignored local QA files and test logs are under `logs/ui09_*`; they are not source
changes proposed for a commit. The temporary loopback preview server was stopped.

## Branding and focused layout changes

The wordmark uses white Clip and the existing yellow Cash on navy. The light
variant puts the same accessible colors on a compact navy capsule. It reuses the
existing yellow play-icon favicon, with an empty decorative image alternative
and one accessible ClipCash name. It appears in the header, public/member
footers and shared authentication introduction, including sign-in/registration.

Existing page grids, cards and workflows are retained. Focused layout work adds
aligned wordmark spacing, a touch-sized language selector and dismiss control,
compact desktop toast positioning, wrapping long text, bounded scrollable stacks
and narrower mobile-safe positioning. The existing spacing/type/color tokens
are reused; no inline CSS, framework or monolithic stylesheet was introduced.

## English and Kiswahili

The standard Django catalogue contains **1,175 translated entries** and 15 plural
entries. The MO is included so the existing catalogue-gated selector offers both
languages immediately. The accessible shared form identifies EN — English and
SW — Kiswahili, retains POST/CSRF and safe redirect handling, and uses Django's
language cookie. The cookie now lasts one year, uses SameSite=Lax and follows the
existing session-cookie Secure setting.

All extractable strings in the following template areas have catalogue entries:
accounts, members, shared components, payments, memberships, rewards, viewing,
videos, support, wallets, withdrawals, advertising, public pages and inbox.
Existing backend feedback from account, membership, payment, advertiser, support
and administration views is translated. Standard Django validation continues to
use Django's own translations; custom phone/password and Airtel validation is
covered by the project catalogue.

Manual Airtel instructions describe real plan-fee payments, official business
record verification and activation only after authorized confirmation. They are
not relabelled as simulations. All existing PIN/privacy and financial notices
remain. Credits are explicitly non-cash, separate from the wallet and neither
spendable nor withdrawable. Displayed credit units and the nine credit decision
explanations are localized. TZS amount formatting, machine reason codes, quoted
credit amounts, eligibility, approvals, ledger postings and security protections
are unchanged. No inbox events or emails were removed or replaced.

`scripts/verify_i18n.py --compile` validates and compiles the ordinary PO to MO
without a new runtime dependency; GNU gettext tools were not available in this
Windows environment. Runtime localization is entirely Django/gettext.

## Notifications

The existing message include remains the single source of backend-confirmed
feedback. Existing server-side success/error decisions and inline form errors
are preserved. Four labeled variants use the existing alert colors. Errors use
role=alert; other messages use role=status, with atomic announcements.

With JavaScript, the messages become a compact top-end stack. Each has a native
button, operable by keyboard; Escape closes the focused toast. Focus moves to
the adjacent dismiss control or main content when needed. Ordinary successes
expire after seven seconds. Errors, warnings, information, successes containing
links/controls and explicitly persistent successes remain until dismissed.
Expiry pauses on hover, keyboard focus and hidden-document state and resumes
with the remaining time. Equal severity/text messages are deduplicated on the
server and client; persistent copies take precedence. Repeated includes are
merged into one stack, and initialization is idempotent.

Without JavaScript, feedback stays visible in the normal page flow and unused
dismiss buttons remain hidden. The persistent notification inbox, stored events,
email outbox and inline validation are unchanged.

## Verification

Final test results are recorded below after completion. Commands:

```text
venv/Scripts/python.exe scripts/run_ui09_regressions.py --database test_clipcash_db
venv/Scripts/python.exe scripts/run_ui09_regressions.py --database test_clipcash_db --full
node scripts/verify_ui09_toasts.mjs
venv/Scripts/python.exe scripts/verify_frontend_css.py
venv/Scripts/python.exe scripts/verify_i18n.py --inventory
venv/Scripts/python.exe manage.py check
venv/Scripts/python.exe manage.py makemigrations --check --dry-run
git diff --check
```

The PostgreSQL scripts verify a distinct, existing, initially idle `test_*`
database, hold its advisory lock and use the guarded project runner/keepdb.
Application data is not the test target. The initial database-free manage.py
invocation was refused by the existing runner's database-identity guard; the
subsequent focused/full runs use the guarded isolated PostgreSQL path.

The first full run executed 1,563 tests in 838.922 seconds: seven failures and
three skips. Two failures exposed locale state leaking from the new Swahili
request tests; those tests now restore the previous language. Five deployment
subprocess failures came from a developer HTTP tunnel origin in local `.env`.
The generic production-test fixture now explicitly supplies an empty origins
list (Render adds its own HTTPS origin), so the synthetic deployment tests no
longer inherit local tunnel configuration. The deployment checks and `.env`
are unchanged. The focused runner includes these seven regression cases.

- Final focused PostgreSQL regressions: **263 tests passed**, 110.639 seconds,
  no failures or skips. This includes the seven corrected cases, phone-first
  registration, manual Airtel payments, quoted-credit behavior and inbox tests.
- Final database-free UI-09 component/accessibility checks: **11 passed**.
- Notification JavaScript: **13 behavior scenarios passed** (DOM/clock mocks).
- CSS verification: **45 modules**, **595 unique selector/condition pairs**;
  no repeated declarations, missing references or unreferenced modules.
- Catalogue validation: **1,175 entries**, **15 plural entries**, no fuzzy/empty
  translations, duplicate entries or changed interpolation placeholders; PO/MO
  match. **Zero missing extractable messages** in the covered template areas.
- Django system checks: **no issues**. Migration dry run: **no changes detected**.
- `git diff --check`: **passed**; index remains empty.
- Final full PostgreSQL run: **1,567 tests run**, **1,564 passed**, **3 skipped**,
  **0 failures/errors**, 450.064 seconds. Existing Bash-dependent checks skip
  because a usable Bash executable is unavailable on this Windows host.
  NetPoa files and those tests are unchanged.

Final logs: `logs/ui09_focused.log` and `logs/ui09_full_final.log`. The latter
is the passing run against the final implementation; `logs/ui09_full.log`
retains the earlier failed run for transparency. The final full and focused
counts overlap and should not be added together.

The 13 JavaScript scenarios use deterministic DOM/clock mocks via the available
Node runtime. They are not visual browser tests. Accessibility checks cover
contrast, accessible markup, keyboard/focus behavior and escaping. A real
screen-reader announcement audit and desktop/mobile visual QA were not completed:
the computer-use tool stopped because it could not establish Chrome's current
URL with enough confidence to enforce its policy. UI automation was ended and
no alternative browser-control route was used.

## Remaining translation gaps and review

The extractor reports **1,225 remaining marked project messages** outside the
covered template areas, principally administration pages and model metadata,
plus lower-level service/access feedback. The inventory excludes migrations,
tests and management commands. It is not an exhaustive audit of unmarked
literal strings. Historical inbox content and emails, staff-entered instructions,
plan/video/campaign names and other user-authored content retain their original
language. Existing untranslated lower-level exceptions can still appear in
English. A fluent Kiswahili editorial review remains advisable.

The machine-readable remaining inventory is generated at
`logs/ui09_translation_gaps.json` with `scripts/verify_i18n.py --inventory`.

**GO for a local commit after review**, with the documented translation gaps
and outstanding browser/device/screen-reader QA. This recommendation is limited
to a local commit; it is not a deployment approval or a claim of complete
project-wide Kiswahili localization. No commit is performed by UI-09.
