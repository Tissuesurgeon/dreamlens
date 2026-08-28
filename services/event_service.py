"""Event sync services — DreamDEX adapter → Django models."""

from __future__ import annotations

import logging
import threading
from dataclasses import replace
from decimal import Decimal

from django.conf import settings
from django.core.cache import cache
from django.db import IntegrityError, close_old_connections, transaction
from django.utils import timezone

from apps.events.models import EventContract, EventOutcome
from integrations.dreamdex.adapter import get_adapter
from integrations.dreamdex.exceptions import DreamDEXUnavailable
from integrations.dreamdex.types import EventDTO
from services.event_copy import format_usd_plain

logger = logging.getLogger("dreamlens.services.event")

_EVENTS_FRESH_CACHE_KEY = "dreamdex:events:fresh"
_EVENTS_SYNCING_KEY = "dreamdex:events:syncing"
_event_sync_lock = threading.Lock()

_STATUS_MAP = {
    "live": EventContract.Status.TRADING,
    "trading": EventContract.Status.TRADING,
    "listed": EventContract.Status.LISTED,
    "locked": EventContract.Status.LOCKED,
    "settling": EventContract.Status.SETTLING,
    "resolved": EventContract.Status.RESOLVED,
    "finalized": EventContract.Status.RESOLVED,
    "voided": EventContract.Status.VOIDED,
}

_EVENT_SYNC_FIELDS = [
    "title",
    "description",
    "underlying_asset",
    "expiry_time",
    "trading_start",
    "yes_identifier",
    "no_identifier",
    "yes_symbol",
    "no_symbol",
    "pool_address",
    "market_address",
    "venue_id",
    "status",
    "strike",
    "interval_sec",
    "cumulative_quote_volume",
    "last_price",
    "trade_count",
    "collateral",
    "oracle_question_id",
    "winning_outcome",
    "source",
    "metadata_json",
    "updated_at",
]


def _format_usd(amount: Decimal) -> str:
    return format_usd_plain(amount)


def _event_window(dto: EventDTO) -> str:
    if dto.interval_sec < 3600:
        mins = max(dto.interval_sec // 60, 1)
        return f"{mins} minute" if mins == 1 else f"{mins} minutes"
    if dto.interval_sec < 86400:
        hours = max(dto.interval_sec // 3600, 1)
        return "1 hour" if hours == 1 else f"{hours} hours"
    days = max(dto.interval_sec // 86400, 1)
    return "1 day" if days == 1 else f"{days} days"


def _event_title(dto: EventDTO) -> str:
    window = _event_window(dto)
    if dto.opening_price and dto.opening_price > 0:
        return (
            f"Will {dto.asset} finish above {_format_usd(dto.opening_price)} "
            f"in the next {window}?"
        )
    return f"Will {dto.asset} go up in the next {window}?"


def _event_description(dto: EventDTO) -> str:
    if dto.question and dto.opening_price and dto.opening_price > 0:
        return (
            f"{dto.question.rstrip('.')}. "
            f"Opening price: {_format_usd(dto.opening_price)}. "
            f"Yes if {dto.asset} is at or above that level at expiry."
        )
    if dto.opening_price and dto.opening_price > 0:
        return (
            f"Yes if {dto.asset} finishes at or above "
            f"{_format_usd(dto.opening_price)} at expiry."
        )
    return dto.question or ""


def _normalize_status(status: str) -> str:
    return _STATUS_MAP.get(status.lower(), EventContract.Status.TRADING)


def event_is_resolved(event: EventContract) -> bool:
    if event.status == EventContract.Status.VOIDED:
        return True
    if event.status in (
        EventContract.Status.RESOLVED,
        EventContract.Status.FINALIZED,
    ) and (event.winning_outcome or "").strip():
        return True
    return False


def _coerce_event_dto(dto: EventDTO) -> EventDTO:
    """Indexer clobStatus lags; treat a posted winner / expiry as settled or locked."""
    status = (dto.status or "").lower()
    if dto.winning_outcome:
        return replace(dto, status="Finalized")
    if status == "voided":
        return dto
    expiry = dto.expiry
    if timezone.is_naive(expiry):
        expiry = timezone.make_aware(expiry, timezone.utc)
    if expiry <= timezone.now() and status in {"live", "trading", "listed"}:
        return replace(dto, status="Locked")
    return dto


def _event_defaults(dto: EventDTO) -> dict:
    dto = _coerce_event_dto(dto)
    metadata = {
        "market_id": dto.market_id,
        "cumulative_base_volume": str(dto.cumulative_base_volume),
    }
    if dto.opening_price is not None:
        metadata["opening_price"] = str(dto.opening_price)
    return {
        "title": _event_title(dto),
        "description": _event_description(dto),
        "underlying_asset": dto.asset,
        "expiry_time": dto.expiry,
        "trading_start": dto.trading_start,
        "yes_identifier": dto.yes_token_id,
        "no_identifier": dto.no_token_id,
        "yes_symbol": dto.yes_symbol,
        "no_symbol": dto.no_symbol,
        "pool_address": dto.pool_address or "",
        "market_address": dto.market_address or "",
        "venue_id": dto.venue_id,
        "status": _normalize_status(dto.status),
        "strike": dto.strike,
        "interval_sec": dto.interval_sec,
        "cumulative_quote_volume": dto.cumulative_quote_volume,
        "last_price": dto.last_price,
        "trade_count": dto.trade_count,
        "collateral": dto.collateral or "",
        "oracle_question_id": dto.oracle_question_id or "",
        "winning_outcome": dto.winning_outcome or "",
        "source": "dreamdex",
        "metadata_json": metadata,
    }


def _sync_interval() -> int:
    return max(int(settings.DREAMDEX_EVENT_SYNC_INTERVAL or 60), 5)


def _has_local_live_events() -> bool:
    return EventContract.objects.filter(
        status__in=[EventContract.Status.TRADING, EventContract.Status.LIVE],
        expiry_time__gt=timezone.now(),
    ).exists()


def _upsert_event(dto: EventDTO) -> tuple[EventContract, bool]:
    contract, created = EventContract.objects.update_or_create(
        external_id=dto.market_id,
        defaults=_event_defaults(dto),
    )
    _upsert_outcomes(contract, dto)
    return contract, created


def _upsert_events_bulk(dtos: list[EventDTO]) -> tuple[int, int]:
    """One read + bulk write instead of N Neon round-trips per market."""
    if not dtos:
        return 0, 0
    now = timezone.now()
    # Last write wins if the adapter lists the same market twice. A single
    # INSERT with duplicate unique keys also fails ON CONFLICT DO UPDATE.
    by_id = {dto.market_id: dto for dto in dtos}
    dtos = list(by_id.values())
    ids = list(by_id.keys())
    existing = {
        row.external_id: row
        for row in EventContract.objects.filter(external_id__in=ids)
    }
    to_create: list[EventContract] = []
    to_update: list[EventContract] = []
    for dto in dtos:
        fields = _event_defaults(dto)
        row = existing.get(dto.market_id)
        if row is None:
            created = EventContract(external_id=dto.market_id, **fields)
            created.updated_at = now
            to_create.append(created)
            continue
        for key, value in fields.items():
            setattr(row, key, value)
        row.updated_at = now
        to_update.append(row)
    if to_create:
        _bulk_create_events(to_create)
    if to_update:
        EventContract.objects.bulk_update(to_update, _EVENT_SYNC_FIELDS)

    contracts = list(EventContract.objects.filter(external_id__in=ids))
    by_ext = {row.external_id: row for row in contracts}
    existing_oc = {
        (row.event_id, row.outcome_type): row
        for row in EventOutcome.objects.filter(event__in=contracts)
    }
    oc_create: list[EventOutcome] = []
    oc_update: list[EventOutcome] = []
    for dto in dtos:
        contract = by_ext[dto.market_id]
        for outcome_type, token_id, symbol, price in (
            (EventOutcome.OutcomeType.YES, dto.yes_token_id, dto.yes_symbol, dto.yes_price),
            (EventOutcome.OutcomeType.NO, dto.no_token_id, dto.no_symbol, dto.no_price),
        ):
            current = existing_oc.get((contract.pk, outcome_type))
            if current is None:
                oc_create.append(
                    EventOutcome(
                        event=contract,
                        outcome_type=outcome_type,
                        external_identifier=token_id,
                        symbol=symbol,
                        current_price=price,
                    )
                )
                continue
            current.external_identifier = token_id
            current.symbol = symbol
            current.current_price = price
            current.updated_at = now
            oc_update.append(current)
    if oc_create:
        _bulk_create_outcomes(oc_create)
    if oc_update:
        EventOutcome.objects.bulk_update(
            oc_update,
            ["external_identifier", "symbol", "current_price", "updated_at"],
        )
    return len(to_create), len(to_update)


def _bulk_create_events(to_create: list[EventContract]) -> None:
    """Insert markets; treat a concurrent sync's commit as an update."""
    try:
        EventContract.objects.bulk_create(
            to_create,
            update_conflicts=True,
            unique_fields=["external_id"],
            update_fields=_EVENT_SYNC_FIELDS,
        )
    except IntegrityError:
        EventContract.objects.bulk_create(to_create, ignore_conflicts=True)


def _bulk_create_outcomes(oc_create: list[EventOutcome]) -> None:
    try:
        EventOutcome.objects.bulk_create(
            oc_create,
            update_conflicts=True,
            unique_fields=["event", "outcome_type"],
            update_fields=["external_identifier", "symbol", "current_price", "updated_at"],
        )
    except IntegrityError:
        EventOutcome.objects.bulk_create(oc_create, ignore_conflicts=True)


def _upsert_outcomes(contract: EventContract, dto: EventDTO) -> None:
    for outcome_type, token_id, symbol, price in (
        (EventOutcome.OutcomeType.YES, dto.yes_token_id, dto.yes_symbol, dto.yes_price),
        (EventOutcome.OutcomeType.NO, dto.no_token_id, dto.no_symbol, dto.no_price),
    ):
        EventOutcome.objects.update_or_create(
            event=contract,
            outcome_type=outcome_type,
            defaults={
                "external_identifier": token_id,
                "symbol": symbol,
                "current_price": price,
            },
        )


@transaction.atomic
def sync_events(*, venue_id: str | None = None, include_finalized: bool = False) -> dict[str, int]:
    """Pull live markets from DreamDEX and upsert EventContract + EventOutcome rows."""
    adapter = get_adapter()
    venue = venue_id or settings.DREAMDEX_VENUE_ID
    events = adapter.list_events(venue_id=venue, status="live")
    live_ids = {dto.market_id for dto in events}

    created, updated = _upsert_events_bulk(events)

    # Markets that left the live list are settled, voided, or still locking.
    # Re-fetch them so winning_outcome is stored instead of only flipping LOCKED.
    stale_qs = EventContract.objects.filter(
        status__in=[EventContract.Status.TRADING, EventContract.Status.LIVE],
    ).exclude(external_id__in=live_ids)
    stale = _refresh_dropped_live_markets(stale_qs)

    finalized_count = 0
    if include_finalized:
        finalized = adapter.list_finalized_events(venue_id=venue)
        finalized_count = len(finalized)
        extra_created, extra_updated = _upsert_events_bulk(finalized)
        created += extra_created
        updated += extra_updated

    logger.info(
        "sync_events complete venue=%s live=%s finalized=%s created=%s updated=%s stale=%s",
        venue,
        len(events),
        finalized_count,
        created,
        updated,
        stale,
    )
    return {
        "created": created,
        "updated": updated,
        "stale": stale,
        "total": created + updated,
    }


def _mark_events_fresh(interval: int) -> None:
    try:
        cache.set(_EVENTS_FRESH_CACHE_KEY, timezone.now().isoformat(), timeout=interval)
    except Exception:
        logger.warning("Failed to write event freshness cache")


def _run_event_sync_job(interval: int) -> None:
    try:
        close_old_connections()
        sync_events()
        _mark_events_fresh(interval)
    except DreamDEXUnavailable:
        logger.exception("DreamDEX unavailable during market refresh")
    except Exception:
        logger.exception("Failed to refresh markets from DreamDEX")
    finally:
        try:
            cache.delete(_EVENTS_SYNCING_KEY)
        except Exception:
            pass
        try:
            close_old_connections()
        except Exception:
            pass
        if _event_sync_lock.locked():
            _event_sync_lock.release()


def _kick_background_event_sync(interval: int) -> None:
    if not _event_sync_lock.acquire(blocking=False):
        return
    try:
        if cache.get(_EVENTS_SYNCING_KEY):
            _event_sync_lock.release()
            return
        cache.set(_EVENTS_SYNCING_KEY, "1", timeout=120)
    except Exception:
        logger.warning("Event sync lock cache unavailable")
    thread = threading.Thread(
        target=_run_event_sync_job,
        args=(interval,),
        daemon=True,
        name="dreamlens-event-sync",
    )
    thread.start()


def refresh_events_from_dreamdex(*, force: bool = False) -> dict[str, int] | None:
    """
    Ensure the local market index mirrors DreamDEX.

    Page views serve local rows immediately. Indexer/Neon sync runs in the
    background when we already have live markets. First load (empty DB) still
    waits so Explore is not blank.
    """
    interval = _sync_interval()
    has_live = _has_local_live_events()
    if not force:
        try:
            if has_live and cache.get(_EVENTS_FRESH_CACHE_KEY):
                return None
        except Exception:
            logger.warning("Event freshness cache unavailable")
        if has_live:
            _kick_background_event_sync(interval)
            return None
    acquired = _event_sync_lock.acquire(blocking=True, timeout=45)
    if not acquired:
        logger.warning("Timed out waiting for DreamDEX market sync")
        return None
    try:
        # Another request may have filled the index while we waited.
        if not force and _has_local_live_events():
            _mark_events_fresh(interval)
            return None
        stats = sync_events()
        _mark_events_fresh(interval)
        return stats
    except DreamDEXUnavailable:
        logger.exception("DreamDEX unavailable during market refresh")
        return None
    except Exception:
        logger.exception("Failed to refresh markets from DreamDEX")
        return None
    finally:
        _event_sync_lock.release()


def _refresh_dropped_live_markets(stale_qs) -> int:
    """Fetch dropped live markets individually; lock only those we cannot read."""
    adapter = get_adapter()
    rows = list(stale_qs.values_list("pk", "external_id")[:40])
    fetched_pks = [pk for pk, _ in rows]
    unresolved: list[int] = []
    for _pk, external_id in rows:
        try:
            dto = adapter.get_event(external_id)
            _upsert_event(dto)
        except Exception:
            logger.warning(
                "stale market refresh failed id=%s",
                external_id,
                exc_info=True,
            )
            unresolved.append(_pk)
    leftover = list(stale_qs.exclude(pk__in=fetched_pks).values_list("pk", flat=True))
    lock_ids = unresolved + leftover
    if lock_ids:
        EventContract.objects.filter(pk__in=lock_ids).update(
            status=EventContract.Status.LOCKED
        )
    return len(lock_ids)


def refresh_event_from_dreamdex(
    event: EventContract,
    *,
    force: bool = False,
) -> EventContract:
    """Re-fetch a single market from DreamDEX and upsert local rows."""
    if not event.external_id:
        return event
    interval = _sync_interval()
    if (
        not force
        and event.updated_at
        and (timezone.now() - event.updated_at).total_seconds() < interval
    ):
        return event
    try:
        dto = get_adapter().get_event(event.external_id)
        contract, _ = _upsert_event(dto)
        return contract
    except Exception:
        logger.exception(
            "Failed to refresh event %s (%s) from DreamDEX",
            event.pk,
            event.external_id,
        )
        return event


def refresh_user_position_events(user) -> int:
    """Re-fetch markets the user traded so portfolio can show won / lost / void."""
    from apps.trading.models import Trade

    event_ids = (
        Trade.objects.filter(user=user, status=Trade.Status.CONFIRMED)
        .values_list("event_id", flat=True)
        .distinct()
    )
    refreshed = 0
    for event in EventContract.objects.filter(pk__in=event_ids):
        if event_is_resolved(event):
            continue
        refresh_event_from_dreamdex(event, force=True)
        refreshed += 1
    return refreshed


@transaction.atomic
def sync_event_prices(*, venue_id: str | None = None) -> dict[str, int]:
    """Refresh prices and volume for tracked live events."""
    adapter = get_adapter()

    venue = venue_id or settings.DREAMDEX_VENUE_ID
    live_events = adapter.list_events(venue_id=venue, status="live")
    dto_by_id = {dto.market_id: dto for dto in live_events}

    updated = 0
    now = timezone.now()
    contracts = EventContract.objects.filter(
        external_id__in=dto_by_id.keys(),
        status__in=[
            EventContract.Status.TRADING,
            EventContract.Status.LIVE,
        ],
    )
    for contract in contracts:
        dto = dto_by_id.get(contract.external_id)
        if not dto:
            continue
        contract.cumulative_quote_volume = dto.cumulative_quote_volume
        contract.last_price = dto.last_price
        contract.trade_count = dto.trade_count
        contract.expiry_time = dto.expiry
        contract.updated_at = now
        contract.save(
            update_fields=[
                "cumulative_quote_volume",
                "last_price",
                "trade_count",
                "expiry_time",
                "updated_at",
            ]
        )
        EventOutcome.objects.filter(
            event=contract,
            outcome_type=EventOutcome.OutcomeType.YES,
        ).update(current_price=dto.yes_price, updated_at=now)
        EventOutcome.objects.filter(
            event=contract,
            outcome_type=EventOutcome.OutcomeType.NO,
        ).update(current_price=dto.no_price, updated_at=now)
        updated += 1

    logger.info("sync_event_prices updated=%s", updated)
    return {"updated": updated}
