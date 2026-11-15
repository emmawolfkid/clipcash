from django.conf import settings
from django.core.checks import Error, register


@register()
def check_payments_mode(app_configs, **kwargs):
    if settings.PAYMENTS_MODE not in ("sandbox", "disabled"):
        return [Error("DJANGO_PAYMENTS_MODE must be 'sandbox' or 'disabled'. Live payments do not exist yet.",
                      id="payments.E001")]
    return []


@register(deploy=True)
def check_sandbox_in_production(app_configs, **kwargs):
    # Deploy-only (the test runner forces DEBUG=False). At runtime sandbox_active()
    # refuses sandbox payments outside DEBUG anyway.
    if settings.PAYMENTS_MODE == "sandbox" and not (settings.DEBUG or settings.PAYMENTS_SANDBOX_ALLOWED):
        return [Error("Sandbox payments are refused in production.",
                      hint="Use DJANGO_PAYMENTS_MODE=disabled, or DJANGO_PAYMENTS_SANDBOX_ALLOWED=True only on an "
                           "isolated demo/staging deployment.", id="payments.E002")]
    return []


@register(deploy=True)
def check_manual_membership_payments(app_configs, **kwargs):
    """PAY-01: real (manual Airtel Lipa) membership payments never share a deployment
    with the restricted demonstration or with sandbox payments. The runtime gate
    (apps.payments.manual.manual_payments_enabled) refuses these combinations too."""
    if not settings.MANUAL_MEMBERSHIP_PAYMENTS_ENABLED:
        return []
    errors = []
    if settings.RESTRICTED_DEMO_MODE:
        errors.append(Error(
            "Manual membership payments are enabled in a restricted demonstration.",
            hint="Real payments are refused while DJANGO_RESTRICTED_DEMO=True. Unset "
                 "DJANGO_MANUAL_MEMBERSHIP_PAYMENTS, or complete the reviewed production launch.",
            id="payments.E003"))
    if settings.PAYMENTS_MODE == "sandbox":
        errors.append(Error(
            "Manual membership payments are enabled together with sandbox payments.",
            hint="Set DJANGO_PAYMENTS_MODE=disabled: sandbox and real membership payments must not coexist.",
            id="payments.E004"))
    return errors


def _inside(path, parent):
    """True when ``path`` is ``parent`` or below it (both resolved; never touches the files)."""
    import os

    path, parent = os.path.normcase(os.path.realpath(path)), os.path.normcase(os.path.realpath(parent))
    return path == parent or path.startswith(parent.rstrip(os.sep) + os.sep)


@register(deploy=True)
def check_payment_evidence_storage(app_configs, **kwargs):
    """PRD-04: receipt evidence holds members' personal and financial data. It must never be
    web-served (not under STATIC_ROOT or MEDIA_ROOT) and, in production, must survive releases:
    every NetPoa release replaces the application directory (rsync --delete)."""
    import os

    if not settings.MANUAL_MEMBERSHIP_PAYMENTS_ENABLED:
        return []
    root = str(settings.PAYMENTS_EVIDENCE_ROOT)
    problems = []
    if not os.path.isabs(root):
        problems.append("it is not an absolute path")
    for name in ("STATIC_ROOT", "MEDIA_ROOT"):
        public = getattr(settings, name, None)
        if public and _inside(root, public):
            problems.append(f"it is inside {name}, which can be served publicly")
    if not settings.DEBUG and _inside(root, settings.BASE_DIR):
        problems.append("it is inside the application directory, which each release replaces")
    if problems:
        return [Error("DJANGO_PAYMENTS_EVIDENCE_ROOT is unsafe: " + "; ".join(problems) + ".",
                      hint="Use a private directory outside the application and the web root, e.g. "
                           "/home/<account>/clipcash/private/payment_evidence (mode 700).", id="payments.E005")]
    return []
