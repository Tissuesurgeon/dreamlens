import os

from .base import *  # noqa: F403

DEBUG = False

_hosts = env.list(  # noqa: F405
    "ALLOWED_HOSTS",
    default=["localhost", "127.0.0.1"],
)
_vercel_url = (os.environ.get("VERCEL_URL") or "").strip()
if _vercel_url:
    _hosts.append(_vercel_url)
    _hosts.append(".vercel.app")
ALLOWED_HOSTS = list(dict.fromkeys(h for h in _hosts if h))

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
SECURE_SSL_REDIRECT = env.bool(  # noqa: F405
    "SECURE_SSL_REDIRECT",
    default=not bool(os.environ.get("VERCEL")),
)
CSRF_COOKIE_SECURE = True
SESSION_COOKIE_SECURE = True

if _vercel_url:
    CSRF_TRUSTED_ORIGINS = list(
        dict.fromkeys(
            list(CSRF_TRUSTED_ORIGINS)  # noqa: F405
            + [f"https://{_vercel_url}", "https://*.vercel.app"]
        )
    )

# Serverless: no Redis, no sticky DB sockets, no 120s import tunnel.
if os.environ.get("VERCEL"):
    CACHES = {  # noqa: F405
        "default": {
            "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
            "LOCATION": "dreamlens-vercel",
        }
    }
    SESSION_ENGINE = "django.contrib.sessions.backends.db"
    DATABASES["default"]["CONN_MAX_AGE"] = 0  # noqa: F405
