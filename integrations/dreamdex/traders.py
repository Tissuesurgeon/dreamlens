"""Thin wrappers around the DreamDEX adapter for trader activity."""

from __future__ import annotations

from integrations.dreamdex.adapter import get_adapter
from integrations.dreamdex.types import FillDTO


def get_fills(
    pool: str,
    *,
    since: int | None = None,
    market_id: str | None = None,
) -> list[FillDTO]:
    return get_adapter().get_fills(pool, since=since, market_id=market_id)


def get_user_fills(account: str, *, pool: str | None = None) -> list[FillDTO]:
    return get_adapter().get_user_fills(account, pool=pool)
