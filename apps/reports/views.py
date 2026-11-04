"""
Staff reports in the administrator console (/manage/reports/). Each report needs its
own permission. Viewing is read-only; a CSV export is a POST (CSRF-protected), also
needs reports.export_reports, and is audited.
"""

from django.contrib import messages
from django.core.exceptions import PermissionDenied
from django.http import Http404, HttpResponse
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils.http import urlencode
from django.utils.translation import gettext as _
from django.views import View

from apps.administration.views import console, console_links

from . import services

CRUMBS = [("Overview", "administration:overview"), ("Reports", "administration:reports")]


def _known(slug):
    if slug not in services.REPORT_TYPES:
        raise Http404


@console
class ReportListView(View):
    def get(self, request):
        reports = services.visible_reports(request.user)
        if not reports:
            raise PermissionDenied
        return render(request, "administration/reports/list.html", {
            "reports": reports, "can": console_links(request.user), "nav": "reports",
            "breadcrumbs": [("Overview", "administration:overview")]})


@console
class ReportView(View):
    def get(self, request, slug):
        _known(slug)
        if not services.can_view(request.user, slug):
            raise PermissionDenied
        error = ""
        try:
            period = services.period_from(request.GET.get("from", "")[:10], request.GET.get("to", "")[:10])
        except services.PeriodError as problem:
            error, period = str(problem), services.period_from()
        return render(request, "administration/reports/detail.html", {
            "report": services.build(slug, period), "period": period, "period_error": error,
            "can_export": services.can_export(request.user, slug), "minimum": services.privacy_minimum(),
            "can": console_links(request.user), "nav": "reports", "breadcrumbs": CRUMBS},
            status=400 if error else 200)


@console
class ReportExportView(View):
    def post(self, request, slug):
        _known(slug)
        if not services.can_export(request.user, slug):
            services.deny(request.user, "export", slug)
            raise PermissionDenied
        first, last = request.POST.get("from", "")[:10], request.POST.get("to", "")[:10]
        back = f"{reverse('administration:report', args=[slug])}?{urlencode({'from': first, 'to': last})}"
        try:
            period = services.period_from(first, last)
        except services.PeriodError as problem:
            messages.error(request, str(problem))
            return redirect(back)
        if services.EXPORTS_PER_HOUR.is_limited(request.user.pk):
            services.deny(request.user, "export", slug, "rate_limited")
            messages.error(request, _("You have exported many reports recently. Please wait a while."))
            return redirect(back)
        services.EXPORTS_PER_HOUR.hit(request.user.pk)
        content = services.export_csv(request.user, slug, period)
        response = HttpResponse(content, content_type="text/csv; charset=utf-8")
        response["Content-Disposition"] = (
            f'attachment; filename="clipcash-{slug}-{period.first.isoformat()}-{period.last.isoformat()}.csv"')
        response["Cache-Control"] = "no-store"
        return response
