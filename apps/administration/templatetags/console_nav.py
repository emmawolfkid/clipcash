"""OPS-01: the console sidebar (permission-aware sections built by apps.administration.navigation)."""

from django import template

from apps.administration import navigation

register = template.Library()


@register.inclusion_tag("administration/_sidebar.html", takes_context=True)
def console_sidebar(context):
    request = context.get("request")
    user = getattr(request, "user", None)
    from apps.administration.views import console_links

    can = context.get("can")
    if not isinstance(can, dict):  # Pages normally pass console_links(user); compute it otherwise.
        can = console_links(user)
    can = {"staff": True, **can}  # Console pages are staff-only (staff_member_required).
    active = navigation.current_item(request, context.get("nav", ""))
    return {"groups": navigation.build(user, can, active), "request": request,
            "unread_notifications": context.get("unread_notifications")}
