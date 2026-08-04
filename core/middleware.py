from django.conf import settings

from .models import site_config_for


class SiteHeadCSPMiddleware:
    """Widen the CSP to cover whatever SiteConfig.head_html loads.

    Scripts on this site are nonce-based and same-origin plus a short list of
    CDNs, which is the right default and also means an analytics snippet pasted
    into the admin does nothing at all: the browser blocks it and reports the
    failure to a console nobody has open. The hosts listed in
    `SiteConfig.script_hosts` are added to script-src and connect-src — the
    second because an analytics script that cannot post its events is as broken
    as one that cannot load.

    This must be listed *after* django.middleware.csp.ContentSecurityPolicyMiddleware
    in MIDDLEWARE. Responses travel back up the list, so "after" here means
    "runs first on the way out", which is what puts `_csp_config` on the
    response before the CSP middleware reads it.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)

        # A view that set its own policy has a reason to; leave it alone.
        if hasattr(response, "_csp_config"):
            return response

        # SiteConfig.head_html only ever lands in an HTML document, so nothing
        # else needs the policy widened. This guard is load-bearing beyond the
        # saved query: /healthz is text/plain and must not touch the database.
        # It used to, through this middleware, which meant a stalled database
        # failed the liveness probe and turned a degraded site into a restart
        # loop. tests/test_smoke.py holds the line.
        if "text/html" not in response.headers.get("Content-Type", ""):
            return response

        try:
            hosts = site_config_for(request).script_host_list
        except Exception:
            # The CSP is not worth a 500. This runs on every response,
            # including the ones served while the database is unreachable or
            # migrations have not run yet — during which the default policy
            # from settings applies, unchanged.
            return response

        if not hosts:
            return response

        config = {key: list(value) for key, value in settings.SECURE_CSP.items()}
        for directive in ("script-src", "connect-src"):
            config[directive] = config.get(directive, []) + hosts
        response._csp_config = config
        return response
