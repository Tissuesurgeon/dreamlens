"""Thin wrappers around the DreamDEX adapter for event discovery."""

from __future__ import annotations

from integrations.dreamdex.adapter import get_adapter
from integrations.dreamdex.types import EventDTO


def list_live_events(*, venue_id: str | None = None) -> list[EventDTO]:
    return get_adapter().list_events(venue_id=venue_id, status="live")


def list_finalized_events(*, venue_id: str | None = None) -> list[EventDTO]:
    return get_adapter().list_finalized_events(venue_id=venue_id)


def get_event(market_id: str) -> EventDTO:
    return get_adapter().get_event(market_id)


def get_market_onchain(market_id: str):
    return get_adapter().get_market_onchain(market_id)
