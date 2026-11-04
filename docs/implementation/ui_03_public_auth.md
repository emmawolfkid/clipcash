# UI-03 public website and authentication

## Scope

The homepage is a public editorial landing page: navy hero, explicit demonstration
notice, six-step account/viewing journey, member benefits, advertiser approval
introduction, native FAQ disclosures and working footer links. The play graphic
is labelled as an introduction, not a playable video. No stock statistics,
testimonials, balances, legal URLs or replacement logo are introduced.

`public/layout.html` customizes new base navigation/footer blocks. Portal layouts
retain their existing defaults. `accounts/layout.html` inherits the public chrome
and provides a shared form/content panel for all 20 account entry, verification,
recovery and result templates. The member profile remains in its portal layout.

`static/css/layouts/marketing.css` contains only public/auth chrome additions;
`layouts/auth.css` contains the account shell. The homepage retains its own
`pages/public/home.css`. Shared controls and tokens are reused, and no empty
per-account page stylesheets are created.

The shared form component renders password controls only when the auth layout
sets `password_controls`. Buttons remain hidden without JavaScript. `auth.js`
changes password visibility and submission feedback only: input names, values,
hidden fields, CSRF and server-side validation are unchanged. It guards repeated
submissions, keeps native fields enabled for serialization and resets feedback
and password masking on `pageshow` (including back/forward restoration).

New copy uses Django translation tags. The Swahili brand tagline and language
switching are retained. This does not constitute a completed Swahili translation
catalogue or a human linguistic review.

## Automated checks

```powershell
& .\venv\Scripts\python.exe -B manage.py test apps.core.tests.test_design_system apps.core.tests.test_public_auth_ui --testrunner django.test.runner.DiscoverRunner --noinput
& .\venv\Scripts\python.exe -B scripts/verify_frontend_css.py
& .\venv\Scripts\python.exe -B manage.py check
& .\venv\Scripts\python.exe -B scripts/run_ui03_regressions.py --database test_clipcash_ui03_20261104
```

The UI-03 wrapper reuses the existing guarded PostgreSQL runner, adds presentation
tests and runs the complete accounts test package, including registration,
verification, recovery, access and throttling. Use a separate, provisioned, idle
`test_*` database. Existing databases are retained. No production migrations or
records are changed by this workflow.

## Manual browser verification still required

Recorded automated results:

- Isolated PostgreSQL suite: 466 tests passed in 194.624 seconds, no failures or
  errors; database preserved. This includes all account tests and UI-02 regressions.
- After the final language-selector extraction: 15 focused template/language
  tests passed, including safe language-cookie switching for English and Swahili.
- Seven authentication JavaScript checks passed using DOM mocks; these are not
  browser or assistive-technology tests.
- CSS check: 26 nonempty modules and 379 unique selector/condition pairs, with
  no repeated properties, missing references or unreferenced modules.
- Django system checks and `git diff --check` passed.

Browser access was unavailable during implementation. No screenshot, visual
responsive pass or screen-reader pass is claimed.

At **320, 375, 390, 768, 1024 and 1440 CSS pixels**:

1. Open `/` and `/accounts/login/`. Check full-page horizontal overflow, headline
   wrapping, hero illustration, FAQ, footer, forms and long labels/messages.
2. Tab from the skip link through navigation, language controls and page actions.
   At narrow widths, open each menu, press Escape and confirm focus returns to its
   toggle. Check desktop navigation at both sides of the 52rem breakpoint.
3. Expand and collapse FAQ items with keyboard controls. Confirm every anchor
   reaches the corresponding section and feature-gated links disappear when off.
4. On login, toggle password visibility with mouse and keyboard. Confirm labels
   and pressed state follow the input and password-manager/autofill still work.
5. Submit invalid account forms. Check field labels, linked error summary,
   announced errors, focus visibility and server error text without clipped UI.
6. Check loading feedback and browser back/forward recovery. Verify the form is
   usable again and passwords return to a masked state.
7. Check open/closed registration, verification required, resend, invalid/expired
   activation/reset links, completion states, password change and logout. Use only
   dedicated test accounts and test tokens, not production account mutations.
8. Switch English/Swahili, test longer translated content, zoom to 200% and 400%,
   and inspect high-contrast/reduced-motion settings and screen-reader names.
9. Confirm the no-cash-value notice remains prominent and no sample balance or
   promise of earnings appears.

The pre-existing UI-02 and unfinished support changes remain unstaged. UI-04 is
outside this phase.
