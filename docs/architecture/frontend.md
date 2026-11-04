# ClipCash frontend styles (UI-02)

The frontend uses Django Templates and external CSS/JavaScript. No bundler,
framework, inline styles, or third-party CSS dependency is required.

## Ownership and loading

- `static/css/core/`: brand/semantic tokens, resets/accessibility and typography.
- `static/css/components/`: shared buttons, forms, cards, tables, status badges,
  notices, lists/activity, media, navigation and the notification bell.
- `static/css/layouts/`: site chrome, reusable portal shell, advertiser tabs and
  shared administration count lists. Administration reuses the portal shell.
- `static/css/pages/`: existing page-only rules. Create a file only when a page
  has unique styles; do not copy component declarations into a page stylesheet.

`templates/base.html` exposes `stylesheets`, `layout_styles` and `page_styles`.
The reusable `components/stylesheets.html` include loads core, shared components,
then site chrome. Portal layouts override `layout_styles`; the advertiser layout
extends the member layout using `block.super`. Each page with unique rules
overrides `page_styles` and loads its own module once. Keep this order.

The protected notification inbox still inherits `base.html`. Its layout and
page styles are selected there using its resolved namespace/view name, without
editing the inbox source. The bell is a shared component, not inbox-only CSS.

Example for a future page with genuinely unique styling:

```django
{% extends "members/layout.html" %}{% load static %}
{% block page_styles %}
<link rel="stylesheet" href="{% static 'css/pages/example/detail.css' %}">
{% endblock %}
```

Do not add an empty stylesheet just to match a directory convention. Shared
video cards, wallet summaries, financial notices and activity lists remain
components even when multiple feature areas use them. The old root-level
`clipcash.css`, `tokens.css` and `design-system.css` are retired.

## Checks

```powershell
& .\venv\Scripts\python.exe -B scripts/verify_frontend_css.py
& .\venv\Scripts\python.exe -B manage.py check
& .\venv\Scripts\python.exe -B manage.py test apps.core.tests.test_design_system --testrunner django.test.runner.DiscoverRunner --noinput
& .\venv\Scripts\python.exe -B scripts/run_ui02_regressions.py --database test_clipcash_ui02_css_20261104
```

The regression wrapper requires an existing, idle `test_*` PostgreSQL database
distinct from the application database. It keeps an advisory lock during the
single run, refuses pre-existing sessions, requires the project's guarded runner,
and uses `keepdb=True`. It never creates or removes databases. Its test database
name override exists only in the process environment. Do not run another suite
against the same database concurrently. Initial test schema setup uses the
existing migration files; production schema/data are not changed.

Structural and template checks do not establish a visual or WCAG conformance
pass. Browser verification must separately cover 320, 375, 390, 768, 1024 and
1440 CSS pixels, keyboard interactions, tables and translated content.
