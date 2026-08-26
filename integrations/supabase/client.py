"""Supabase Python client over HTTPS (PostgREST / Auth).

This path works when the Postgres wire protocol to the pooler is blocked.
It is not a Django database backend — ORM models still use DATABASE_URL.
"""

from __future__ import annotations

from typing import Any

import httpx
from django.conf import settings
from postgrest.exceptions import APIError
from supabase import Client, create_client

_client: Client | None = None


class SupabaseError(Exception):
    pass


class SupabaseConfigError(SupabaseError):
    pass


def configured() -> bool:
    return bool(_url() and _key())


def reset_client() -> None:
    global _client
    _client = None


def _url() -> str:
    return (getattr(settings, "SUPABASE_URL", "") or "").strip().rstrip("/")


def _key() -> str:
    return (getattr(settings, "SUPABASE_KEY", "") or "").strip()


def get_client() -> Client:
    global _client
    if _client is not None:
        return _client
    url = _url()
    key = _key()
    if not url or not key:
        raise SupabaseConfigError("SUPABASE_URL and SUPABASE_KEY are required")
    _client = create_client(url, key)
    return _client


def ping() -> dict[str, Any]:
    """Confirm the Data API accepts this project URL and publishable key."""
    url = _url()
    key = _key()
    if not url or not key:
        raise SupabaseConfigError("SUPABASE_URL and SUPABASE_KEY are required")
    health = httpx.get(
        f"{url}/auth/v1/health",
        headers={"apikey": key, "Authorization": f"Bearer {key}"},
        timeout=10,
    )
    health.raise_for_status()
    client = get_client()
    try:
        todos = client.table("todos").select("*").limit(5).execute()
        todos_data = list(todos.data or [])
        todos_error = None
    except APIError as exc:
        todos_data = []
        todos_error = exc.message or str(exc)
    return {
        "url": url,
        "auth_health": health.json() if health.content else {"ok": True},
        "todos": todos_data,
        "todos_error": todos_error,
    }
