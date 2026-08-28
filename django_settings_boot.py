"""Set DJANGO_SETTINGS_MODULE before importing the config package.

``config/__init__.py`` imports Celery, which used to setdefault local settings.
This module must be imported first from wsgi/asgi/manage.py so a Railway
(or Render/Vercel) process never waits on the local Postgres tunnel.
"""

from __future__ import annotations

import os

_HOSTED_MARKERS = (
    "VERCEL",
    "RAILWAY_ENVIRONMENT",
    "RAILWAY_ENVIRONMENT_ID",
    "RAILWAY_PUBLIC_DOMAIN",
    "RAILWAY_PROJECT_ID",
    "RAILWAY_SERVICE_ID",
    "RAILWAY_STATIC_URL",
    "RENDER",
)


def hosted_edge() -> bool:
    if any(os.environ.get(key) for key in _HOSTED_MARKERS):
        return True
    return any(key.startswith("RAILWAY_") for key in os.environ)


def configure_django_settings() -> None:
    existing = os.environ.get("DJANGO_SETTINGS_MODULE") or ""
    if existing.endswith(".test"):
        return
    if hosted_edge():
        os.environ["DJANGO_SETTINGS_MODULE"] = "config.settings.production"
        return
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings.local")
