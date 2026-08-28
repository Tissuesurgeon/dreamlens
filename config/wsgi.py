"""
WSGI config for DreamLens.

It exposes the WSGI callable as a module-level variable named ``application``.
"""

from django_settings_boot import configure_django_settings

configure_django_settings()

from django.core.wsgi import get_wsgi_application  # noqa: E402

application = get_wsgi_application()
