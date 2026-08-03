from django.http import HttpResponse
from django.shortcuts import render

from core.models import SiteConfig


def index(request):
    return render(request, "web/index.html", {"site_config": SiteConfig.load()})


def healthz(request):
    """Liveness probe. Deliberately does not touch the database — a slow query
    should not take the pod out of rotation."""
    return HttpResponse("ok", content_type="text/plain")
