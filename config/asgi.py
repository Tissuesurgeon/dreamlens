"""
ASGI config for DreamLens.

It exposes the ASGI callable as a module-level variable named ``application``.
"""

import os

from django.core.asgi import get_asgi_application

if os.environ.get("VERCEL"):
    os.environ["DJANGO_SETTINGS_MODULE"] = "config.settings.production"
else:
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings.local")

application = get_asgi_application()
