"""
ASGI config for DreamLens.

It exposes the ASGI callable as a module-level variable named ``application``.
"""

from django_settings_boot import configure_django_settings

configure_django_settings()

from django.core.asgi import get_asgi_application  # noqa: E402

application = get_asgi_application()
