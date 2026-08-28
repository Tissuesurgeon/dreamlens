"""Hosted-runtime helpers. Safe to import after Django settings are chosen."""

from __future__ import annotations

import os

RAILWAY_HOST_WILDCARDS = (".up.railway.app", ".railway.internal")
RAILWAY_CSRF_ORIGINS = ("https://*.up.railway.app",)


def redis_looks_local(url: str) -> bool:
    """True when Redis is the local/compose host, not a hosted plugin."""
    from urllib.parse import urlparse

    host = (urlparse(url or "").hostname or "").lower()
    return host in {"localhost", "127.0.0.1", "0.0.0.0", "::1", "redis"}


def _host_from_url(value: str) -> str:
    value = (value or "").strip()
    if not value:
        return ""
    return value.split("//")[-1].split("/")[0].split(":")[0]


def _on_railway() -> bool:
    return any(key.startswith("RAILWAY_") for key in os.environ)


def public_host_aliases() -> list[str]:
    hosts: list[str] = []
    vercel = (os.environ.get("VERCEL_URL") or "").strip()
    if vercel:
        hosts.append(_host_from_url(vercel))
        hosts.append(".vercel.app")
    for key in ("RAILWAY_PUBLIC_DOMAIN", "RAILWAY_STATIC_URL"):
        host = _host_from_url(os.environ.get(key, ""))
        if host:
            hosts.append(host)
    if _on_railway():
        hosts.extend(RAILWAY_HOST_WILDCARDS)
        for key, value in os.environ.items():
            if not key.startswith("RAILWAY_") or ".up.railway.app" not in (value or ""):
                continue
            host = _host_from_url(value)
            if host.endswith(".up.railway.app") or host == "up.railway.app":
                hosts.append(host)
    render = (os.environ.get("RENDER_EXTERNAL_HOSTNAME") or "").strip()
    if render:
        hosts.append(render)
        hosts.append(".onrender.com")
    return list(dict.fromkeys(h for h in hosts if h))


def csrf_origin_aliases() -> list[str]:
    origins: list[str] = []
    vercel = (os.environ.get("VERCEL_URL") or "").strip()
    if vercel:
        origins.append(f"https://{_host_from_url(vercel)}")
        origins.append("https://*.vercel.app")
    railway = _host_from_url(os.environ.get("RAILWAY_PUBLIC_DOMAIN", ""))
    if railway:
        origins.append(f"https://{railway}")
    if _on_railway() or railway:
        origins.extend(RAILWAY_CSRF_ORIGINS)
    render = (os.environ.get("RENDER_EXTERNAL_HOSTNAME") or "").strip()
    if render:
        origins.append(f"https://{render}")
        origins.append("https://*.onrender.com")
    return list(dict.fromkeys(origins))


def merge_allowed_hosts(configured: list[str], *, always_railway: bool = False) -> list[str]:
    hosts = [str(h).strip() for h in configured if h and str(h).strip()]
    hosts.extend(public_host_aliases())
    if always_railway or _on_railway():
        hosts.extend(RAILWAY_HOST_WILDCARDS)
    return list(dict.fromkeys(hosts))
