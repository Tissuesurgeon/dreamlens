"""Railway/Vercel boot helpers — no Django settings import required."""

from __future__ import annotations

import os

from config.runtime import (
    csrf_origin_aliases,
    merge_allowed_hosts,
    public_host_aliases,
    redis_looks_local,
)
from django_settings_boot import configure_django_settings, hosted_edge


def test_railway_forces_production(monkeypatch):
    monkeypatch.delenv("DJANGO_SETTINGS_MODULE", raising=False)
    monkeypatch.delenv("VERCEL", raising=False)
    monkeypatch.delenv("RENDER", raising=False)
    monkeypatch.delenv("RAILWAY_PUBLIC_DOMAIN", raising=False)
    monkeypatch.setenv("RAILWAY_ENVIRONMENT", "production")
    configure_django_settings()
    assert os.environ["DJANGO_SETTINGS_MODULE"] == "config.settings.production"
    assert hosted_edge()


def test_test_settings_not_overridden_on_railway(monkeypatch):
    monkeypatch.setenv("DJANGO_SETTINGS_MODULE", "config.settings.test")
    monkeypatch.setenv("RAILWAY_ENVIRONMENT", "production")
    configure_django_settings()
    assert os.environ["DJANGO_SETTINGS_MODULE"] == "config.settings.test"


def test_local_default_when_not_hosted(monkeypatch):
    monkeypatch.delenv("DJANGO_SETTINGS_MODULE", raising=False)
    for key in list(os.environ):
        if key.startswith("RAILWAY_") or key in ("VERCEL", "RENDER"):
            monkeypatch.delenv(key, raising=False)
    configure_django_settings()
    assert os.environ["DJANGO_SETTINGS_MODULE"] == "config.settings.local"
    assert not hosted_edge()


def test_railway_hosts_and_csrf(monkeypatch):
    monkeypatch.setenv("RAILWAY_ENVIRONMENT", "production")
    monkeypatch.setenv("RAILWAY_PUBLIC_DOMAIN", "dreamlens-production-2447.up.railway.app")
    monkeypatch.delenv("VERCEL_URL", raising=False)
    monkeypatch.delenv("RENDER_EXTERNAL_HOSTNAME", raising=False)
    hosts = public_host_aliases()
    assert "dreamlens-production-2447.up.railway.app" in hosts
    assert ".up.railway.app" in hosts
    origins = csrf_origin_aliases()
    assert "https://dreamlens-production-2447.up.railway.app" in origins
    assert "https://*.up.railway.app" in origins


def test_any_railway_var_counts_as_hosted(monkeypatch):
    monkeypatch.delenv("DJANGO_SETTINGS_MODULE", raising=False)
    for key in ("VERCEL", "RAILWAY_ENVIRONMENT", "RAILWAY_PUBLIC_DOMAIN", "RENDER"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("RAILWAY_PROJECT_ID", "proj_test")
    assert hosted_edge()
    configure_django_settings()
    assert os.environ["DJANGO_SETTINGS_MODULE"] == "config.settings.production"
    hosts = merge_allowed_hosts(["localhost"], always_railway=True)
    assert ".up.railway.app" in hosts
    assert "localhost" in hosts


def test_redis_local_detection():
    assert redis_looks_local("redis://localhost:6379/0")
    assert redis_looks_local("redis://127.0.0.1:6379/1")
    assert not redis_looks_local("redis://redis.railway.internal:6379/0")
