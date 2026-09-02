"""Trader sync and scoring from DreamDEX fills."""

from __future__ import annotations

import logging
import re
import threading
from collections import defaultdict
from datetime import timedelta
from decimal import Decimal, ROUND_HALF_UP

from django.conf import settings
from django.core.cache import cache
from django.db import close_old_connections, transaction
from django.utils import timezone

from apps.dreamcopy.models import CopyRelationship, TraderProfile, TraderTrade
from apps.events.models import EventContract, EventOutcome
from integrations.dreamdex.adapter import get_adapter
from integrations.dreamdex.exceptions import DreamDEXUnavailable
from integrations.dreamdex.mock import TRADER_WALLETS

logger = logging.getLogger("dreamlens.services.trader")

_ONCHAIN_ADDRESS = re.compile(r"^0x[0-9a-f]{40}$")
_ZERO_ADDRESS = "0x" + "0" * 40
_SEED_WALLETS = {address.lower() for address in TRADER_WALLETS.values()}
_TRADERS_FRESH_CACHE_KEY = "dreamdex:traders:fresh"
_TRADERS_SYNCING_KEY = "dreamdex:traders:syncing"
_trader_sync_lock = threading.Lock()

MIN_COMPLETED_TRADES = 5
FOUR_PLACES = Decimal("0.0001")

# Deterministic weights for trader score
WEIGHT_WIN_RATE = Decimal("0.35")
WEIGHT_ROI = Decimal("0.30")
WEIGHT_SAMPLE = Decimal("0.20")
WEIGHT_CONSISTENCY = Decimal("0.15")


def compute_trader_score(
    *,
    win_rate: Decimal,
    roi: Decimal,
    completed_trades: int,
    consistency: Decimal | None = None,
) -> Decimal:
    """Deterministic composite score in roughly [0, 1]."""
    sample_factor = min(Decimal(completed_trades) / Decimal("50"), Decimal("1"))
    roi_norm = min(max(roi + Decimal("0.5"), Decimal("0")), Decimal("1"))
    consistency_score = consistency if consistency is not None else win_rate

    score = (
        win_rate * WEIGHT_WIN_RATE
        + roi_norm * WEIGHT_ROI
        + sample_factor * WEIGHT_SAMPLE
        + consistency_score * WEIGHT_CONSISTENCY
    )
    return score.quantize(FOUR_PLACES, rounding=ROUND_HALF_UP)


def _wallet_from_fill(fill) -> str | None:
    return fill.maker or fill.taker or None


def is_onchain_trader_wallet(address: str | None) -> bool:
    """True for a real EVM address that is not a DreamLens mock seed wallet."""
    if not address:
        return False
    normalized = address.lower()
    if normalized in _SEED_WALLETS or normalized == _ZERO_ADDRESS:
        return False
    return bool(_ONCHAIN_ADDRESS.match(normalized))


def normalize_trader_wallet(address: str) -> str:
    """Lowercase 0x + 40 hex. Rejects empty, zero, and non-EVM strings."""
    normalized = (address or "").strip().lower()
    if not _ONCHAIN_ADDRESS.match(normalized) or normalized == _ZERO_ADDRESS:
        raise ValueError("Enter a valid 0x wallet address.")
    return normalized


def ensure_trader_profile(address: str) -> TraderProfile:
    """Return the profile for this wallet, creating a stub if it is not indexed yet."""
    addr = normalize_trader_wallet(address)
    existing = TraderProfile.objects.filter(wallet_address__iexact=addr).first()
    if existing:
        return existing
    return TraderProfile.objects.create(wallet_address=addr, display_name=addr[:10])


def purge_seed_traders() -> int:
    """Remove mock/seed trader rows left over from earlier demo syncs."""
    removed = 0
    for trader in TraderProfile.objects.all().iterator():
        if is_onchain_trader_wallet(trader.wallet_address):
            continue
        trader.delete()
        removed += 1
    if removed:
        logger.info("purged seed/invalid traders count=%s", removed)
    return removed


def list_active_traders(*, limit: int | None = None) -> list[TraderProfile]:
    """Every indexed on-chain DreamDEX fill participant, live markets first."""
    refresh_traders_from_dreamdex()
    picked: list[TraderProfile] = []
    seen: set[int] = set()

    live = (
        TraderProfile.objects.filter(
            total_trades__gt=0,
            trades__event__status__in=[
                EventContract.Status.TRADING,
                EventContract.Status.LIVE,
            ],
        )
        .order_by("-total_volume", "-total_trades", "-trader_score")
        .distinct()
    )
    for trader in live:
        if trader.pk in seen or not is_onchain_trader_wallet(trader.wallet_address):
            continue
        picked.append(trader)
        seen.add(trader.pk)
        if limit is not None and len(picked) >= limit:
            return picked

    extras = (
        TraderProfile.objects.filter(total_trades__gt=0)
        .exclude(pk__in=seen)
        .order_by("-total_volume", "-total_trades", "-trader_score")
    )
    for trader in extras:
        if not is_onchain_trader_wallet(trader.wallet_address):
            continue
        picked.append(trader)
        if limit is not None and len(picked) >= limit:
            break
    return picked


def list_suggested_traders(*, limit: int | None = None) -> list[TraderProfile]:
    """Alias for list_active_traders (kept for existing imports/tests)."""
    return list_active_traders(limit=limit)


def _fill_notional(trade: TraderTrade) -> Decimal:
    amount = trade.amount or Decimal("0")
    price = trade.entry_price or Decimal("0")
    notional = amount * price
    return notional if notional > 0 else amount


def build_trader_analytics(trader: TraderProfile, *, history_limit: int = 250) -> dict:
    """Aggregate indexed fills for the trader analytics page."""
    trades = list(
        TraderTrade.objects.filter(trader=trader)
        .select_related("event", "outcome")
        .order_by("-opened_at")[:history_limit]
    )
    indexed_count = TraderTrade.objects.filter(trader=trader).count()

    yes_count = 0
    no_count = 0
    yes_volume = Decimal("0")
    no_volume = Decimal("0")
    asset_stats: dict[str, dict] = defaultdict(
        lambda: {"count": 0, "volume": Decimal("0")}
    )
    daily: dict[str, Decimal] = defaultdict(lambda: Decimal("0"))
    event_ids: set[int] = set()
    total_notional = Decimal("0")

    for trade in trades:
        notional = _fill_notional(trade)
        total_notional += notional
        event_ids.add(trade.event_id)
        side = (trade.outcome.outcome_type or "").upper()
        if side == "YES":
            yes_count += 1
            yes_volume += notional
        elif side == "NO":
            no_count += 1
            no_volume += notional
        asset = (trade.event.underlying_asset or "—").upper()
        asset_stats[asset]["count"] += 1
        asset_stats[asset]["volume"] += notional
        day = timezone.localtime(trade.opened_at).date().isoformat()
        daily[day] += notional

    side_total = yes_volume + no_volume
    yes_share = float(yes_volume / side_total) if side_total else 0.0
    no_share = float(no_volume / side_total) if side_total else 0.0
    asset_rows = sorted(
        (
            {
                "asset": asset,
                "count": stats["count"],
                "volume": stats["volume"],
                "share": float(stats["volume"] / total_notional) if total_notional else 0,
            }
            for asset, stats in asset_stats.items()
        ),
        key=lambda row: row["volume"],
        reverse=True,
    )
    days = sorted(daily.keys())
    fill_count = len(trades)
    avg_fill = Decimal("0")
    if indexed_count:
        volume_for_avg = trader.total_volume or total_notional
        avg_fill = (volume_for_avg / Decimal(indexed_count))
    win_pct = trader.win_rate or Decimal("0")
    if win_pct <= 1:
        win_pct = win_pct * 100
    score_100 = (trader.trader_score or Decimal("0")) * 100
    if score_100 > 100:
        score_100 = trader.trader_score or Decimal("0")

    last_fill_at = trades[0].opened_at if trades else trader.last_active_at
    roi = trader.roi or Decimal("0")
    has_roi = bool(trader.realized_pnl) or roi != 0
    volume = trader.total_volume or total_notional
    max_day = max(daily.values()) if daily else Decimal("0")

    return {
        "fills": trades[:80],
        "fill_sample": fill_count,
        "indexed_count": indexed_count,
        "unique_markets": len(event_ids),
        "volume": volume,
        "avg_fill": avg_fill,
        "yes_count": yes_count,
        "no_count": no_count,
        "yes_volume": yes_volume,
        "no_volume": no_volume,
        "yes_share": yes_share,
        "no_share": no_share,
        "assets": asset_rows[:8],
        "win_pct": win_pct,
        "score_100": score_100,
        "has_roi": has_roi,
        "last_fill_at": last_fill_at,
        "chart": {
            "labels": days,
            "volumes": [float(daily[d]) for d in days],
            "rows": [
                {
                    "label": day,
                    "volume": daily[day],
                    "pct": float(daily[day] / max_day * 100) if max_day else 0,
                }
                for day in days
            ],
        },
    }


def refresh_traders_from_dreamdex(*, force: bool = False) -> dict[str, int] | None:
    """Re-index maker/taker wallets from DreamDEX fills on the event sync TTL."""
    from services.event_service import refresh_events_from_dreamdex

    refresh_events_from_dreamdex(force=force)

    interval = max(int(getattr(settings, "DREAMDEX_EVENT_SYNC_INTERVAL", 60) or 60), 5)
    has_traders = TraderProfile.objects.filter(total_trades__gt=0).exists()
    if not force:
        try:
            if has_traders and cache.get(_TRADERS_FRESH_CACHE_KEY):
                return None
        except Exception:
            logger.warning("Trader freshness cache unavailable")
        if has_traders:
            _kick_background_trader_sync(interval)
            return None
    try:
        purge_seed_traders()
        stats = sync_traders_from_fills()
        try:
            cache.set(_TRADERS_FRESH_CACHE_KEY, timezone.now().isoformat(), timeout=interval)
        except Exception:
            logger.warning("Failed to write trader freshness cache")
        return stats
    except DreamDEXUnavailable:
        logger.exception("DreamDEX unavailable during trader refresh")
        return None
    except Exception:
        logger.exception("Failed to refresh traders from DreamDEX")
        return None


def _kick_background_trader_sync(interval: int) -> None:
    if not _trader_sync_lock.acquire(blocking=False):
        return
    try:
        if cache.get(_TRADERS_SYNCING_KEY):
            _trader_sync_lock.release()
            return
        cache.set(_TRADERS_SYNCING_KEY, "1", timeout=120)
    except Exception:
        logger.warning("Trader sync lock cache unavailable")

    def _run() -> None:
        try:
            close_old_connections()
            from services.event_service import refresh_events_from_dreamdex

            refresh_events_from_dreamdex()
            purge_seed_traders()
            sync_traders_from_fills()
            cache.set(_TRADERS_FRESH_CACHE_KEY, timezone.now().isoformat(), timeout=interval)
        except Exception:
            logger.exception("Background trader refresh failed")
        finally:
            try:
                cache.delete(_TRADERS_SYNCING_KEY)
            except Exception:
                pass
            try:
                close_old_connections()
            except Exception:
                pass
            if _trader_sync_lock.locked():
                _trader_sync_lock.release()

    threading.Thread(target=_run, daemon=True, name="dreamlens-trader-sync").start()


def _outcome_for_side(event: EventContract, side: str) -> EventOutcome | None:
    outcome_type = side.upper()
    if outcome_type not in ("YES", "NO"):
        return None
    for outcome in event.outcomes.all():
        if outcome.outcome_type == outcome_type:
            return outcome
    return None


def _fill_belongs_to_event(fill, event: EventContract) -> bool:
    mid = (getattr(fill, "market_id", None) or "").strip().lower()
    if not mid.startswith("0x") or set(mid[2:]) <= {"0"}:
        return False
    want = (event.external_id or "").strip().lower()
    return bool(want) and mid == want


def sync_traders_from_fills() -> dict[str, int]:
    """Index fills from known events into TraderProfile + TraderTrade."""
    adapter = get_adapter()
    events = list(
        EventContract.objects.filter(
            pool_address__gt="",
            status__in=[
                EventContract.Status.TRADING,
                EventContract.Status.LIVE,
            ],
        )
        .prefetch_related("outcomes")
        .order_by("-trade_count")[:80]
    )
    events_by_pool = {
        event.pool_address.lower(): event for event in events if event.pool_address
    }
    events_by_market = {
        (event.external_id or "").lower(): event
        for event in events
        if event.external_id
    }

    fills_by_event: dict[int, list] = defaultdict(list)
    for event in events:
        try:
            for fill in adapter.get_fills(
                event.pool_address,
                market_id=event.external_id or None,
            ):
                if not _fill_belongs_to_event(fill, event):
                    continue
                fills_by_event[event.pk].append(fill)
        except DreamDEXUnavailable:
            raise
        except Exception:
            logger.warning("get_fills failed pool=%s", event.pool_address, exc_info=True)

    get_recent = getattr(adapter, "get_recent_fills", None)
    if callable(get_recent):
        try:
            for fill in get_recent(limit=300) or []:
                mid = (getattr(fill, "market_id", None) or "").lower()
                event = events_by_market.get(mid) or events_by_pool.get(
                    (fill.pool or "").lower()
                )
                if event and _fill_belongs_to_event(fill, event):
                    fills_by_event[event.pk].append(fill)
        except Exception:
            logger.warning("get_recent_fills failed", exc_info=True)

    with transaction.atomic():
        result = _write_indexed_fills(events, fills_by_event)
    copy_executions = _process_recent_copy_trades(result.pop("newly_created_ids"))
    result["copy_executions"] = copy_executions
    return result


def _write_indexed_fills(events, fills_by_event: dict[int, list]) -> dict:
    wallet_stats: dict[str, dict] = {}
    pending_trades: list[dict] = []
    seen_fill_ids: set[str] = set()
    seen_external: set[str] = set()

    for event in events:
        for fill in fills_by_event.get(event.pk, []):
            fill_key = f"{fill.id}:{event.pk}"
            if fill_key in seen_fill_ids:
                continue
            seen_fill_ids.add(fill_key)

            for wallet, side in (
                (fill.maker, fill.maker_side),
                (fill.taker, fill.taker_side),
            ):
                if not is_onchain_trader_wallet(wallet):
                    continue
                outcome = _outcome_for_side(event, side)
                if not outcome:
                    continue
                addr = wallet.lower()
                external_id = f"{fill.id}:{addr}"
                if external_id in seen_external:
                    continue
                seen_external.add(external_id)

                stats = wallet_stats.setdefault(
                    addr,
                    {
                        "total_trades": 0,
                        "completed_trades": 0,
                        "winning_trades": 0,
                        "losing_trades": 0,
                        "total_volume": Decimal("0"),
                        "realized_pnl": Decimal("0"),
                    },
                )
                stats["total_trades"] += 1
                stats["total_volume"] += fill.quote_quantity
                stats["completed_trades"] += 1
                pending_trades.append(
                    {
                        "wallet": addr,
                        "event": event,
                        "outcome": outcome,
                        "entry_price": fill.fill_price,
                        "amount": fill.quantity,
                        "opened_at": fill.timestamp,
                        "transaction_hash": fill.tx_hash,
                        "external_trade_id": external_id,
                    }
                )

    if not wallet_stats:
        logger.info("sync_traders_from_fills profiles_created=0 trades_created=0")
        return {
            "profiles_created": 0,
            "trades_created": 0,
            "newly_created_ids": [],
        }

    addresses = list(wallet_stats.keys())
    existing_profiles: dict[str, TraderProfile] = {}
    for row in TraderProfile.objects.filter(wallet_address__in=addresses):
        existing_profiles[row.wallet_address.lower()] = row
    missing = [addr for addr in addresses if addr not in existing_profiles]
    if missing:
        from django.db.models import Q

        lookup = Q()
        for addr in missing:
            lookup |= Q(wallet_address__iexact=addr)
        for row in TraderProfile.objects.filter(lookup):
            existing_profiles[row.wallet_address.lower()] = row
    new_profiles = [
        TraderProfile(wallet_address=addr, display_name=addr[:10])
        for addr in addresses
        if addr not in existing_profiles
    ]
    if new_profiles:
        TraderProfile.objects.bulk_create(new_profiles, ignore_conflicts=True, batch_size=200)
        existing_profiles = {}
        for row in TraderProfile.objects.filter(wallet_address__in=addresses):
            existing_profiles[row.wallet_address.lower()] = row
        still_missing = [addr for addr in addresses if addr not in existing_profiles]
        if still_missing:
            from django.db.models import Q

            lookup = Q()
            for addr in still_missing:
                lookup |= Q(wallet_address__iexact=addr)
            for row in TraderProfile.objects.filter(lookup):
                existing_profiles[row.wallet_address.lower()] = row

    existing_external = set(
        TraderTrade.objects.filter(
            external_trade_id__in=[row["external_trade_id"] for row in pending_trades]
        ).values_list("external_trade_id", flat=True)
    )
    trades_to_create: list[TraderTrade] = []
    for row in pending_trades:
        if row["external_trade_id"] in existing_external:
            continue
        trader = existing_profiles.get(row["wallet"])
        if not trader:
            continue
        trades_to_create.append(
            TraderTrade(
                trader=trader,
                event=row["event"],
                outcome=row["outcome"],
                entry_price=row["entry_price"],
                amount=row["amount"],
                opened_at=row["opened_at"],
                transaction_hash=row["transaction_hash"],
                external_trade_id=row["external_trade_id"],
            )
        )
    if trades_to_create:
        TraderTrade.objects.bulk_create(trades_to_create, batch_size=200)

    now = timezone.now()
    for addr, stats in wallet_stats.items():
        trader = existing_profiles.get(addr)
        if not trader:
            continue
        completed = stats["completed_trades"]
        win_rate = Decimal(stats["winning_trades"] / completed) if completed else Decimal("0")
        volume = stats["total_volume"]
        pnl = stats["realized_pnl"]
        roi = pnl / volume if volume else Decimal("0")
        trader.total_trades = stats["total_trades"]
        trader.completed_trades = completed
        trader.winning_trades = stats["winning_trades"]
        trader.losing_trades = stats["losing_trades"]
        trader.win_rate = win_rate.quantize(FOUR_PLACES)
        trader.total_volume = volume
        trader.realized_pnl = pnl
        trader.roi = roi.quantize(FOUR_PLACES)
        trader.last_active_at = now
        trader.trader_score = compute_trader_score(
            win_rate=trader.win_rate,
            roi=trader.roi,
            completed_trades=trader.completed_trades,
        )
    TraderProfile.objects.bulk_update(
        list(existing_profiles.values()),
        [
            "total_trades",
            "completed_trades",
            "winning_trades",
            "losing_trades",
            "win_rate",
            "total_volume",
            "realized_pnl",
            "roi",
            "last_active_at",
            "trader_score",
        ],
        batch_size=200,
    )

    recent_cutoff = now - timedelta(minutes=45)
    new_external = [row.external_trade_id for row in trades_to_create]
    created_ids = list(
        TraderTrade.objects.filter(external_trade_id__in=new_external).values_list(
            "pk", flat=True
        )
    ) if new_external else []
    followed_ids = list(
        CopyRelationship.objects.filter(
            status=CopyRelationship.Status.ACTIVE
        ).values_list("trader_id", flat=True)
    )
    replay_ids = []
    if followed_ids:
        replay_ids = list(
            TraderTrade.objects.filter(
                trader_id__in=followed_ids,
                opened_at__gte=recent_cutoff,
            ).values_list("pk", flat=True)
        )
    newly_created_ids = list({*created_ids, *replay_ids})

    logger.info(
        "sync_traders_from_fills profiles_created=%s trades_created=%s",
        len(new_profiles),
        len(trades_to_create),
    )
    return {
        "profiles_created": len(new_profiles),
        "trades_created": len(trades_to_create),
        "newly_created_ids": newly_created_ids,
    }


def _process_recent_copy_trades(newly_created_ids: list[int]) -> int:
    """Copy only fresh fills — historical backfill must not block the traders page."""
    if not newly_created_ids:
        return 0
    cutoff = timezone.now() - timedelta(minutes=45)
    from services.copy_service import detect_and_process_copy

    copy_executions = 0
    recent = (
        TraderTrade.objects.filter(pk__in=newly_created_ids, opened_at__gte=cutoff)
        .select_related("trader", "event", "outcome")
    )
    for trade in recent:
        try:
            copy_executions += len(detect_and_process_copy(trade))
        except Exception:
            logger.exception("detect_and_process_copy failed for trade=%s", trade.pk)
    return copy_executions


def is_trader_eligible(trader: TraderProfile) -> bool:
    return (
        trader.completed_trades >= MIN_COMPLETED_TRADES
        and trader.trader_score >= Decimal("0.35")
    )
