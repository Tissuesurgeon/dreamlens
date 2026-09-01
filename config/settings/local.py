import os
import socket
import subprocess
import sys
import time

from config.runtime import csrf_origin_aliases, merge_allowed_hosts
from django_settings_boot import hosted_edge

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
    if hosted_edge() or os.environ.get("AWS_LAMBDA_FUNCTION_NAME"):
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


def _release_session_pooler_slots() -> None:
    """Do not pin session-mode pooler slots (EMAXCONNSESSION, pool_size 15).

    Persistent Django connections plus runserver reload, the Telegram poller,
    and DreamDEX indexer threads exhaust the local tunnel quickly.
    """
    db = DATABASES["default"]  # noqa: F405
    host = (db.get("HOST") or "").lower()
    hostaddr = str((db.get("OPTIONS") or {}).get("hostaddr") or "")
    try:
        port = int(db.get("PORT") or 0)
    except (TypeError, ValueError):
        port = 0
    via_tunnel = port == _PG_TUNNEL_LISTEN[1] or hostaddr in {
        _PG_TUNNEL_LISTEN[0],
        "127.0.0.1",
        "::1",
    }
    via_supabase_pooler = "pooler.supabase.com" in host
    if via_tunnel or via_supabase_pooler:
        db["CONN_MAX_AGE"] = 0
        db["CONN_HEALTH_CHECKS"] = True


_release_session_pooler_slots()


# Local runserver talks to the JSON API from the browser; do not fail POSTs on CSRF.
REST_FRAMEWORK = {
    **REST_FRAMEWORK,  # noqa: F405
    "DEFAULT_AUTHENTICATION_CLASSES": [
        "apps.core.authentication.CsrfExemptSessionAuthentication",
    ],
}

if hosted_edge():
    DEBUG = False

ALLOWED_HOSTS = merge_allowed_hosts(
    env.list(  # noqa: F405
        "ALLOWED_HOSTS",
        default=["localhost", "127.0.0.1", "0.0.0.0"],
    )
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
        + csrf_origin_aliases()
    )
)

# Local runserver should not depend on Redis for login/session or Telegram confirms.
SESSION_ENGINE = "django.contrib.sessions.backends.db"
CACHES = {  # noqa: F405
    "default": {
        "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
        "LOCATION": "dreamlens-local",
    }
}


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
CELERY_BROKER_URL = REDIS_URL
CELERY_RESULT_BACKEND = REDIS_URL

# This machine's LLM is the LAN Ollama — not Google / OpenRouter.
LLM_PROVIDER = "ollama"
LLM_MODEL = "llama3.2"
LLM_BASE_URL = "http://192.168.0.110:11434/v1"
LLM_API_KEY = "local"
LOCAL_LLM_ENABLED = True
LOCAL_LLM_BASE_URL = "http://192.168.0.110:11434/v1"
LOCAL_LLM_API_KEY = "local"
LOCAL_LLM_MODEL = "llama3.2"
