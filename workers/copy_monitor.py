"""Celery worker for monitoring new trader trades for copy / DreamAgent."""

from __future__ import annotations

import logging

from celery import shared_task

from apps.dreamcopy.models import TraderTrade
from services.copy_service import detect_and_process_copy

logger = logging.getLogger("dreamlens.workers.copy_monitor")


@shared_task(name="workers.copy_monitor.process_new_trader_trades")
def process_new_trader_trades(*, since_id: int = 0) -> dict[str, int]:
    """Process trader trades newer than since_id.

    When the copier has a RUNNING DreamAgent, detect_and_process_copy routes
    through Policy → Risk → delegated Smart Account execution (no wallet popup).
    Otherwise it creates PENDING copy executions for user confirmation.
    """
    trades = (
        TraderTrade.objects.filter(pk__gt=since_id)
        .select_related("trader", "event", "outcome")
        .order_by("pk")
    )
    processed = 0
    executions = 0
    last_id = since_id

    for trade in trades:
        results = detect_and_process_copy(trade)
        processed += 1
        executions += len(results)
        last_id = trade.pk

    logger.info(
        "process_new_trader_trades processed=%s executions=%s last_id=%s",
        processed,
        executions,
        last_id,
    )
    return {"processed": processed, "executions": executions, "last_id": last_id}
