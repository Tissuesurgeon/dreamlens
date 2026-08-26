"""Session authentication helpers. Kept out of apps.core.api to avoid DRF import cycles."""

from rest_framework.authentication import SessionAuthentication


class CsrfExemptSessionAuthentication(SessionAuthentication):
    """Session auth without CSRF enforcement for JSON API clients."""

    def enforce_csrf(self, request):
        return
