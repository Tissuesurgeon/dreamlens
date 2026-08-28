import os
import socket
import subprocess
import sys
import time

from .base import *  # noqa: F403

DEBUG = True

_PG_TUNNEL_LISTEN = ("127.0.0.1", 65432)


def _running_tests() -> bool:
    module = os.environ.get("DJANGO_SETTINGS_MODULE", "")
    return "pytest" in sys.modules or "test" in sys.argv or module.endswith(".test")


def _tcp_open(host: str, port: int, timeout: float = 0.3) -> bool:
    try:
        sock = socket.create_connection((host, port), timeout)
        sock.close()
        return True
    except OSError:
        return False


def _postgres_handshake_ok(host: str, port: int, timeout: float = 20.0) -> bool:
    """True when the local tunnel answers libpq's SSLRequest with S."""
    import struct

    try:
        sock = socket.create_connection((host, port), min(timeout, 5))
        sock.settimeout(timeout)
        sock.sendall(struct.pack("!II", 8, 80877103))
        reply = sock.recv(1)
        sock.close()
        return reply == b"S"
    except OSError:
        return False


def _ensure_supabase_pg_tunnel() -> None:
    """Route local Supabase Postgres via Tor when the ISP drops SSLRequest."""
    if os.environ.get("VERCEL") or os.environ.get("AWS_LAMBDA_FUNCTION_NAME"):
        return
    db = DATABASES["default"]  # noqa: F405
    host = (db.get("HOST") or "").lower()
    if "supabase.com" not in host or _running_tests():
        return
    listen_host, listen_port = _PG_TUNNEL_LISTEN
    if not _postgres_handshake_ok(listen_host, listen_port):
        script = BASE_DIR / "scripts" / "supabase_pg_tunnel.py"  # noqa: F405
        log = open("/tmp/dreamlens-pg-tunnel.log", "ab")
        subprocess.Popen(
            [sys.executable, str(script)],
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
            cwd=str(BASE_DIR),  # noqa: F405
        )
        deadline = time.time() + 120
        while time.time() < deadline and not _postgres_handshake_ok(
            listen_host, listen_port, timeout=8
        ):
            time.sleep(2)
    if not _postgres_handshake_ok(listen_host, listen_port, timeout=12):
        return
    db["PORT"] = listen_port
    db.setdefault("OPTIONS", {})["hostaddr"] = listen_host
    db["OPTIONS"]["connect_timeout"] = 60


_ensure_supabase_pg_tunnel()


# Local runserver talks to the JSON API from the browser; do not fail POSTs on CSRF.
REST_FRAMEWORK = {
    **REST_FRAMEWORK,  # noqa: F405
    "DEFAULT_AUTHENTICATION_CLASSES": [
        "apps.core.authentication.CsrfExemptSessionAuthentication",
    ],
}

ALLOWED_HOSTS = env.list(  # noqa: F405
    "ALLOWED_HOSTS",
    default=["localhost", "127.0.0.1", "0.0.0.0"],
)

EMAIL_BACKEND = "django.core.mail.backends.console.EmailBackend"

INTERNAL_IPS = ["127.0.0.1"]

# JSON API calls from the local UI (localhost vs 127.0.0.1) must pass CSRF origin checks.
CSRF_TRUSTED_ORIGINS = list(
    dict.fromkeys(
        list(CSRF_TRUSTED_ORIGINS)  # noqa: F405
        + [
            "http://localhost:8000",
            "http://127.0.0.1:8000",
        ]
    )
)

# Local runserver should not depend on Redis for login/session.
SESSION_ENGINE = "django.contrib.sessions.backends.db"


def _redis_url_for_this_host(url: str) -> str:
    """Compose uses hostname redis; local runserver cannot resolve it."""
    from urllib.parse import urlparse, urlunparse
    import socket

    parsed = urlparse(url)
    host = parsed.hostname or ""
    if host != "redis":
        return url
    try:
        socket.getaddrinfo(host, parsed.port or 6379, socket.AF_INET)
        return url
    except OSError:
        port = parsed.port or 6379
        auth = parsed.username or ""
        if parsed.password:
            auth = f"{auth}:{parsed.password}"
        userinfo = f"{auth}@" if auth else ""
        return urlunparse(parsed._replace(netloc=f"{userinfo}127.0.0.1:{port}"))


REDIS_URL = _redis_url_for_this_host(REDIS_URL)  # noqa: F405
CACHES["default"]["LOCATION"] = REDIS_URL  # noqa: F405
CELERY_BROKER_URL = REDIS_URL
CELERY_RESULT_BACKEND = REDIS_URL
