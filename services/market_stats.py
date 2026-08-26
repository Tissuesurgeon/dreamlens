"""Shared market volume, book liquidity, and event-trader stats."""

from __future__ import annotations

import logging
from collections import defaultdict
from decimal import Decimal, ROUND_DOWN, ROUND_HALF_UP

from apps.dreamcopy.models import TraderProfile
from apps.events.models import EventContract, EventOutcome
from integrations.dreamdex.types import FillDTO

logger = logging.getLogger("dreamlens.services.market_stats")

TWO_PLACES = Decimal("0.01")
FOUR_PLACES = Decimal("0.0001")


def event_market_id(event: EventContract) -> str:
    meta = event.metadata_json or {}
    mid = str(meta.get("market_id") or event.external_id or "").strip()
    return mid


def fill_matches_event(fill: FillDTO, event: EventContract) -> bool:
    """True when the fill's market id is this event — pools are recycled across windows."""
    mid = (getattr(fill, "market_id", None) or "").strip().lower()
    if not mid.startswith("0x"):
        return False
    if set(mid[2:]) <= {"0"}:
        return False
    want = (event.external_id or "").strip().lower()
    meta = str((event.metadata_json or {}).get("market_id") or "").strip().lower()
    return mid == want or (bool(meta) and mid == meta)


def book_liquidity(event: EventContract) -> Decimal:
    """Resting YES book depth as quote notional. Fail open to 0."""
    from integrations.dreamdex.exceptions import DreamDEXNotFound
    from integrations.dreamdex.trading import get_order_book

    keys: list[str] = []
    mid = event_market_id(event)
    if mid:
        keys.append(mid)
    symbol = getattr(event, "yes_symbol", "") or ""
    if symbol and symbol not in keys:
        keys.append(symbol)
    if not keys:
        return Decimal("0")

    book = None
    for key in keys:
        try:
            book = get_order_book(key, depth=8)
            break
        except DreamDEXNotFound:
            continue
        except Exception:
            logger.debug("order book unavailable for event=%s", getattr(event, "pk", None))
            return Decimal("0")
    if book is None:
        return Decimal("0")

    total = Decimal("0")
    for level in list(book.bids or []) + list(book.asks or []):
        qty = level.quantity or Decimal("0")
        price = level.price or Decimal("0")
        total += (qty * price) if price > 0 else qty
    return total.quantize(TWO_PLACES, rounding=ROUND_HALF_UP)


def event_fills(event: EventContract) -> list[FillDTO]:
    if not event.pool_address:
        return []
    try:
        from integrations.dreamdex.traders import get_fills

        fills = get_fills(
            event.pool_address,
            market_id=event_market_id(event) or None,
        )
    except Exception:
        logger.debug("fills unavailable for event=%s", getattr(event, "pk", None))
        return []
    return [fill for fill in fills if fill_matches_event(fill, event)]


def traded_volume(event: EventContract) -> Decimal:
    """Indexer quote volume, falling back to summed market fills."""
    vol = event.cumulative_quote_volume or Decimal("0")
    if vol > 0:
        return vol
    total = sum((f.quote_quantity or Decimal("0") for f in event_fills(event)), Decimal("0"))
    return total


def displayed_trade_count(event: EventContract, *, fill_fallback: bool = False) -> int:
    count = int(event.trade_count or 0)
    if count > 0 or not fill_fallback:
        return count
    return len(event_fills(event))


def _yes_no_prices(event: EventContract) -> tuple[Decimal, Decimal]:
    yes_price = Decimal("0.5")
    no_price = Decimal("0.5")
    for outcome in event.outcomes.all():
        price = outcome.current_price or Decimal("0.5")
        if outcome.outcome_type == EventOutcome.OutcomeType.YES:
            yes_price = price
        elif outcome.outcome_type == EventOutcome.OutcomeType.NO:
            no_price = price
    return yes_price, no_price


def _format_share_qty(qty: Decimal) -> str:
    abs_qty = abs(qty)
    if abs_qty == 0:
        return "0"
    if abs_qty >= 1 and abs_qty == abs_qty.to_integral_value():
        return str(int(abs_qty))
    if abs_qty >= TWO_PLACES:
        return f"{abs_qty.quantize(TWO_PLACES, rounding=ROUND_DOWN)}"
    return format(abs_qty.normalize(), "f")


def _position_label(yes_qty: Decimal, no_qty: Decimal) -> str:
    parts: list[str] = []
    if yes_qty != 0:
        suffix = " short" if yes_qty < 0 else ""
        parts.append(f"{_format_share_qty(yes_qty)} YES{suffix}")
    if no_qty != 0:
        suffix = " short" if no_qty < 0 else ""
        parts.append(f"{_format_share_qty(no_qty)} NO{suffix}")
    return " · ".join(parts)


def list_event_participants(event: EventContract, fills: list[FillDTO] | None = None) -> list[dict]:
    """Wallets that traded this Event Contract, with net YES/NO size on the window."""
    from services.trader_service import is_onchain_trader_wallet

    if fills is None:
        fills = event_fills(event)

    yes_price, no_price = _yes_no_prices(event)
    wallets: dict[str, dict] = defaultdict(
        lambda: {
            "yes_qty": Decimal("0"),
            "no_qty": Decimal("0"),
            "quote": Decimal("0"),
            "fills": 0,
        }
    )
    for fill in fills:
        qty = fill.quantity or Decimal("0")
        quote = fill.quote_quantity or Decimal("0")
        for wallet, side, is_buy in (
            (fill.maker, fill.maker_side, getattr(fill, "maker_is_buy", True)),
            (fill.taker, fill.taker_side, getattr(fill, "taker_is_buy", True)),
        ):
            if not is_onchain_trader_wallet(wallet):
                continue
            addr = wallet.lower()
            signed = qty if is_buy else -qty
            if (side or "").upper() == "NO":
                wallets[addr]["no_qty"] += signed
            else:
                wallets[addr]["yes_qty"] += signed
            wallets[addr]["quote"] += quote
            wallets[addr]["fills"] += 1

    addresses = list(wallets)
    profiles = {
        row.wallet_address.lower(): row
        for row in TraderProfile.objects.filter(wallet_address__in=addresses)
    }

    rows: list[dict] = []
    for addr, stats in wallets.items():
        yes_qty = stats["yes_qty"]
        no_qty = stats["no_qty"]
        mark = (abs(yes_qty) * yes_price) + (abs(no_qty) * no_price)
        amount = mark if mark > 0 else stats["quote"]
        profile = profiles.get(addr)
        rows.append(
            {
                "wallet": addr,
                "name": (profile.display_name if profile and profile.display_name else addr),
                "roi": profile.roi if profile else Decimal("0"),
                "win_rate": profile.win_rate if profile else Decimal("0"),
                "total_trades": stats["fills"],
                "volume": stats["quote"],
                "position_amount": amount.quantize(TWO_PLACES, rounding=ROUND_HALF_UP),
                "position_label": _position_label(yes_qty, no_qty),
                "yes_qty": yes_qty,
                "no_qty": no_qty,
                "profile": profile,
            }
        )
    rows.sort(key=lambda row: row["position_amount"], reverse=True)
    return rows


def event_market_stats(event: EventContract) -> dict:
    """Volume, book liquidity, trade count, and this event's traders."""
    fills = event_fills(event)
    indexer_vol = event.cumulative_quote_volume or Decimal("0")
    volume = indexer_vol if indexer_vol > 0 else sum(
        (f.quote_quantity or Decimal("0") for f in fills),
        Decimal("0"),
    )
    trade_count = int(event.trade_count or 0)
    if trade_count <= 0:
        trade_count = len(fills)

    traders = list_event_participants(event, fills=fills)
    yes_notional = sum((abs(row["yes_qty"]) for row in traders), Decimal("0"))
    no_notional = sum((abs(row["no_qty"]) for row in traders), Decimal("0"))
    side_total = yes_notional + no_notional
    if side_total > 0:
        yes_share = (yes_notional / side_total).quantize(FOUR_PLACES, rounding=ROUND_HALF_UP)
    else:
        yes_share = Decimal("0.5")

    return {
        "volume": volume,
        "liquidity": book_liquidity(event),
        "trade_count": trade_count,
        "trader_count": len(traders),
        "traders": traders[:12],
        "yes_position_share": yes_share,
        "no_position_share": (Decimal("1") - yes_share).quantize(
            FOUR_PLACES, rounding=ROUND_HALF_UP
        ),
    }
