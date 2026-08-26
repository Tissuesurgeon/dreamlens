"""Supabase Data API (HTTPS) client for DreamLens."""

from integrations.supabase.client import (
    SupabaseConfigError,
    SupabaseError,
    configured,
    get_client,
    ping,
    reset_client,
)

__all__ = [
    "SupabaseConfigError",
    "SupabaseError",
    "configured",
    "get_client",
    "ping",
    "reset_client",
]
