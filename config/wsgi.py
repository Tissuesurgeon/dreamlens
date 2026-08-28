"""
WSGI config for DreamLens.

It exposes the WSGI callable as a module-level variable named ``application``.
"""

import os
import time

from django_settings_boot import configure_django_settings

_t0 = time.monotonic()


def _boot(msg: str) -> None:
    print(f"dreamlens-boot {msg} +{time.monotonic() - _t0:.1f}s", flush=True)


configure_django_settings()
_boot(f"settings={os.environ.get('DJANGO_SETTINGS_MODULE')}")

from django.core.wsgi import get_wsgi_application  # noqa: E402

application = get_wsgi_application()
_boot("django ready")
