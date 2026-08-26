"""Celery workers for DreamDEX event sync."""

from __future__ import annotations

import logging

from celery import shared_task

from services.event_service import sync_event_prices, sync_events
from services.radar_service import generate_radar_signals

logger = logging.getLogger("dreamlens.workers.event_sync")


@shared_task(name="workers.event_sync.sync_events_task")
def sync_events_task() -> dict[str, int]:
    result = sync_events()
    logger.info("sync_events_task result=%s", result)
    return result


@shared_task(name="workers.event_sync.sync_event_prices_task")
def sync_event_prices_task() -> dict[str, int]:
    result = sync_event_prices()
    logger.info("sync_event_prices_task result=%s", result)
    return result


@shared_task(name="workers.event_sync.generate_radar_signals_task")
def generate_radar_signals_task() -> dict[str, int]:
    result = generate_radar_signals()
    logger.info("generate_radar_signals_task result=%s", result)
    return result


@shared_task(name="workers.event_sync.full_event_sync_task")
def full_event_sync_task() -> dict[str, dict[str, int]]:
    """Run events, prices, radar, and fill/copy processing in sequence."""
    from services.trader_service import sync_traders_from_fills

    return {
        "events": sync_events(),
        "prices": sync_event_prices(),
        "radar": generate_radar_signals(),
        "fills": sync_traders_from_fills(),
    }
