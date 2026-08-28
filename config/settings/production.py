import os

from config.runtime import csrf_origin_aliases, merge_allowed_hosts, redis_looks_local
from django_settings_boot import hosted_edge

from .base import *  # noqa: F403

DEBUG = False

ALLOWED_HOSTS = merge_allowed_hosts(
    env.list("ALLOWED_HOSTS", default=["localhost", "127.0.0.1"]),  # noqa: F405
    always_railway=True,
)

MIDDLEWARE.insert(1, "whitenoise.middleware.WhiteNoiseMiddleware")  # noqa: F405

# Manifest storage 500s if collectstatic did not run (typical on Vercel).
_static_backend = (
    "whitenoise.storage.CompressedStaticFilesStorage"
    if os.environ.get("VERCEL")
    else "whitenoise.storage.CompressedManifestStaticFilesStorage"
)
STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": _static_backend},
}
STATICFILES_STORAGE = _static_backend

SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
# Edge hosts terminate TLS. An HTTP health check must not 301-loop.
SECURE_SSL_REDIRECT = env.bool(  # noqa: F405
    "SECURE_SSL_REDIRECT",
    default=not hosted_edge(),
)
CSRF_COOKIE_SECURE = True
SESSION_COOKIE_SECURE = True

CSRF_TRUSTED_ORIGINS = list(
    dict.fromkeys(
        list(CSRF_TRUSTED_ORIGINS)  # noqa: F405
        + csrf_origin_aliases()
        + ["https://*.up.railway.app"]
    )
)

# Serverless, or a host that never got a Redis plugin: don't block on localhost:6379.
if os.environ.get("VERCEL") or (hosted_edge() and redis_looks_local(REDIS_URL)):  # noqa: F405
    CACHES = {  # noqa: F405
        "default": {
            "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
            "LOCATION": "dreamlens-hosted",
        }
    }
    SESSION_ENGINE = "django.contrib.sessions.backends.db"

if os.environ.get("VERCEL"):
    DATABASES["default"]["CONN_MAX_AGE"] = 0  # noqa: F405
