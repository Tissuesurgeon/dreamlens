"""Event Radar scoring and signal generation."""

from __future__ import annotations

import logging
from decimal import Decimal, ROUND_HALF_UP

from django.db import transaction
from django.utils import timezone

from apps.events.models import EventContract, EventOutcome, EventRadarSignal
from integrations.dreamdex.adapter import get_adapter
from integrations.dreamdex.traders import get_fills

logger = logging.getLogger("dreamlens.services.radar")

FOUR_PLACES = Decimal("0.0001")
VOLUME_THRESHOLD = Decimal("80000")
LIQUIDITY_THRESHOLD = Decimal("40")
MOMENTUM_THRESHOLD = Decimal("0.015")
CONSENSUS_THRESHOLD = Decimal("0.65")
IMBALANCE_THRESHOLD = Decimal("0.15")
EXPIRY_SOON_MINUTES = 15


def compute_interest_score(
    *,
    yes_price: Decimal,
    momentum: Decimal,
    volume: Decimal,
    liquidity: Decimal,
    minutes_to_expiry: float,
) -> Decimal:
    """Deterministic interest score in [0, 1] from market factors."""
    consensus = min(abs(yes_price - Decimal("0.5")) * 2, Decimal("1"))
    momentum_score = min(abs(momentum) * Decimal("20"), Decimal("1"))
    volume_score = min(volume / Decimal("150000"), Decimal("1"))
    liquidity_score = min(liquidity / Decimal("80"), Decimal("1"))
    if minutes_to_expiry <= 0:
        expiry_score = Decimal("1")
    elif minutes_to_expiry >= 60:
        expiry_score = Decimal("0")
    else:
        expiry_score = Decimal("1") - (Decimal(str(minutes_to_expiry)) / Decimal("60"))

    score = (
        consensus * Decimal("0.25")
        + momentum_score * Decimal("0.20")
        + volume_score * Decimal("0.20")
        + liquidity_score * Decimal("0.15")
        + expiry_score * Decimal("0.20")
    )
    return score.quantize(FOUR_PLACES, rounding=ROUND_HALF_UP)


def _yes_outcome(contract: EventContract) -> EventOutcome | None:
    return contract.outcomes.filter(outcome_type=EventOutcome.OutcomeType.YES).first()


def _estimate_liquidity(contract: EventContract) -> Decimal:
    from services.market_stats import book_liquidity

    return book_liquidity(contract)


def _trader_side_ratio(contract: EventContract) -> tuple[int, int]:
    if not contract.pool_address:
        return 0, 0
    yes_count = 0
    no_count = 0
    for fill in get_fills(contract.pool_address):
        if fill.market_id != contract.external_id:
            continue
        if fill.taker_side == "YES" or fill.maker_side == "YES":
            yes_count += 1
        else:
            no_count += 1
    return yes_count, no_count


def _detect_signals(
    contract: EventContract,
    *,
    yes_price: Decimal,
    momentum: Decimal,
    liquidity: Decimal,
    minutes_to_expiry: float,
) -> list[tuple[str, str, dict]]:
    signals: list[tuple[str, str, dict]] = []

    if yes_price >= CONSENSUS_THRESHOLD or yes_price <= (Decimal("1") - CONSENSUS_THRESHOLD):
        direction = "YES" if yes_price >= CONSENSUS_THRESHOLD else "NO"
        signals.append(
            (
                EventRadarSignal.SignalType.STRONG_CONSENSUS,
                f"Strong {direction} consensus at {yes_price}",
                {"yes_price": str(yes_price), "direction": direction},
            )
        )

    if abs(momentum) >= MOMENTUM_THRESHOLD:
        direction = "up" if momentum > 0 else "down"
        signals.append(
            (
                EventRadarSignal.SignalType.MOVING_FAST,
                f"Price moving {direction} (Δ{momentum})",
                {"momentum": str(momentum), "direction": direction},
            )
        )

    if contract.cumulative_quote_volume >= VOLUME_THRESHOLD:
        signals.append(
            (
                EventRadarSignal.SignalType.UNUSUAL_VOLUME,
                f"Volume {contract.cumulative_quote_volume} above threshold",
                {"volume": str(contract.cumulative_quote_volume)},
            )
        )

    if 0 < minutes_to_expiry <= EXPIRY_SOON_MINUTES:
        signals.append(
            (
                EventRadarSignal.SignalType.EXPIRING_SOON,
                f"Expires in {round(minutes_to_expiry, 1)} minutes",
                {"minutes_to_expiry": round(minutes_to_expiry, 1)},
            )
        )

    yes_fills, no_fills = _trader_side_ratio(contract)
    if yes_fills >= 2 and yes_fills >= no_fills * 2:
        signals.append(
            (
                EventRadarSignal.SignalType.TRADER_DIVERGENCE,
                f"Traders skew YES ({yes_fills} vs {no_fills})",
                {"yes_fills": yes_fills, "no_fills": no_fills, "bias": "YES"},
            )
        )
    elif no_fills >= 2 and no_fills >= yes_fills * 2:
        signals.append(
            (
                EventRadarSignal.SignalType.TRADER_DIVERGENCE,
                f"Traders skew NO ({no_fills} vs {yes_fills})",
                {"yes_fills": yes_fills, "no_fills": no_fills, "bias": "NO"},
            )
        )

    if liquidity >= LIQUIDITY_THRESHOLD:
        signals.append(
            (
                EventRadarSignal.SignalType.HIGH_LIQUIDITY,
                f"Order book liquidity {liquidity}",
                {"liquidity": str(liquidity)},
            )
        )

    if abs(yes_price - Decimal("0.5")) >= IMBALANCE_THRESHOLD:
        signals.append(
            (
                EventRadarSignal.SignalType.PRICE_IMBALANCE,
                f"YES price {yes_price} far from fair 0.50",
                {
                    "yes_price": str(yes_price),
                    "distance_from_fair": str(abs(yes_price - Decimal("0.5"))),
                },
            )
        )

    return signals


@transaction.atomic
def generate_radar_signals() -> dict[str, int]:
    """Score live events and persist EventRadarSignal rows."""
    adapter = get_adapter()
    now = timezone.now()

    EventRadarSignal.objects.filter(is_active=True).update(is_active=False)

    contracts = EventContract.objects.filter(
        status__in=[EventContract.Status.TRADING, EventContract.Status.LIVE],
        expiry_time__gt=now,
    ).prefetch_related("outcomes")

    created = 0
    for contract in contracts:
        yes = _yes_outcome(contract)
        if not yes:
            continue

        momentum = Decimal("0")
        if hasattr(adapter, "get_price_momentum"):
            momentum = adapter.get_price_momentum(contract.external_id)

        liquidity = _estimate_liquidity(contract)
        minutes_to_expiry = contract.minutes_to_expiry

        score = compute_interest_score(
            yes_price=yes.current_price,
            momentum=momentum,
            volume=contract.cumulative_quote_volume,
            liquidity=liquidity,
            minutes_to_expiry=minutes_to_expiry,
        )

        for signal_type, explanation, details in _detect_signals(
            contract,
            yes_price=yes.current_price,
            momentum=momentum,
            liquidity=liquidity,
            minutes_to_expiry=minutes_to_expiry,
        ):
            EventRadarSignal.objects.create(
                event=contract,
                signal_type=signal_type,
                score=score,
                explanation=explanation,
                details={**details, "interest_score": str(score)},
                is_active=True,
            )
            created += 1

    logger.info("generate_radar_signals created=%s", created)
    return {"created": created}
