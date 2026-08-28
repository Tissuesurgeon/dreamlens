"""Respond to the platform health check before sessions, Redis, or Postgres."""

from django.http import HttpResponse

HEALTHZ_PATHS = frozenset({"/healthz", "/health"})


class HealthzMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        path = (request.path or "").rstrip("/") or "/"
        if path in HEALTHZ_PATHS:
            return HttpResponse("ok", content_type="text/plain")
        return self.get_response(request)
