"""Shared user-facing copy for web, JS, and Telegram.

Hard rule: Telegram and templates must import these helpers. Do not format
question / cents / expiry / payout labels in a second place.
"""

from __future__ import annotations

from decimal import Decimal, ROUND_HALF_UP
from typing import Any

from django.utils import timezone

from apps.events.models import EventContract, EventOutcome

SCORE_DISCLAIMER = (
    "This score is an analysis signal, not a prediction or guarantee."
)
PAY_LABEL = "You pay"
PAYOUT_LABEL = "Maximum possible payout"
PROFIT_LABEL = "Potential profit"
LOSS_LABEL = "Maximum loss"
TODAY_LABEL = "Today's result"

_ASSET_NAMES = {
    "BTC": "Bitcoin",
    "BITCOIN": "Bitcoin",
    "ETH": "Ethereum",
    "ETHEREUM": "Ethereum",
    "SOL": "Solana",
    "SOLANA": "Solana",
}

_TWO = Decimal("0.01")


def _to_decimal(value) -> Decimal | None:
    if value is None or value == "":
        return None
    try:
        return Decimal(str(value))
    except Exception:
        return None


def asset_display_name(asset: str | None) -> str:
    token = (asset or "").strip()
    if not token:
        return "this asset"
    return _ASSET_NAMES.get(token.upper(), token.upper())


def collateral_ticker() -> str:
    """Quote asset of a DreamDEX Event Contract.

    Mainnet settles in USDso. Shannon uses faucet Test USDC as the stand-in.
    """
    from django.conf import settings

    network = (getattr(settings, "DREAMDEX_NETWORK", None) or "testnet").lower()
    chain_id = int(getattr(settings, "DREAMDEX_CHAIN_ID", 50312) or 50312)
    if network == "mainnet" or chain_id == 5031:
        return "USDso"
    return "USDC"


def format_usd_plain(amount) -> str:
    """USD display for strikes and exact amounts (USDC ≈ USD)."""
    value = _to_decimal(amount)
    if value is None:
        return "—"
    quantized = value.quantize(_TWO, rounding=ROUND_HALF_UP)
    return f"-${abs(quantized):,.2f}" if quantized < 0 else f"${quantized:,.2f}"


def format_collateral(amount, *, compact: bool = False) -> str:
    """Stake, payout, volume, strike — shown as dollars because USDC ≈ USD."""
    value = _to_decimal(amount)
    if value is None:
        return "—"
    if compact and value == value.to_integral_value():
        n = int(value)
        return f"-${abs(n)}" if n < 0 else f"${n}"
    if abs(value) < Decimal("1000"):
        quantized = value.quantize(_TWO, rounding=ROUND_HALF_UP)
        return f"-${abs(quantized):,.2f}" if quantized < 0 else f"${quantized:,.2f}"
    quantized = int(value.quantize(Decimal("1"), rounding=ROUND_HALF_UP))
    return f"-${abs(quantized):,}" if quantized < 0 else f"${quantized:,}"


def as_cents(price) -> str:
    """Format a 0–1 outcome price as dollars, e.g. 0.41 → $0.41."""
    value = _to_decimal(price)
    if value is None:
        return "—"
    quantized = value.quantize(_TWO, rounding=ROUND_HALF_UP)
    return f"${quantized}"


def event_strike_usd(event: EventContract) -> Decimal | None:
    opening = getattr(event, "opening_price", None)
    if opening is not None and opening > 0:
        return Decimal(str(opening))
    strike = getattr(event, "strike", None) or 0
    if strike and int(strike) > 0:
        return (Decimal(int(strike)) / Decimal("100")).quantize(_TWO)
    return None


def event_question(event: EventContract) -> str:
    """Consumer question copy. Never lead with a raw contract title."""
    name = asset_display_name(getattr(event, "underlying_asset", None))
    strike = event_strike_usd(event)
    if strike is not None and strike > 0:
        return f"Will {name} be above {format_usd_plain(strike)} at expiry?"
    title = (getattr(event, "title", None) or "").strip()
    if title.lower().startswith("will "):
        return title
    return f"Will {name} finish above the strike at expiry?"


def yes_no_outcomes(event: EventContract) -> tuple[EventOutcome | None, EventOutcome | None]:
    yes = no = None
    outcomes = getattr(event, "outcomes", None)
    if outcomes is None:
        return None, None
    rows = outcomes.all() if hasattr(outcomes, "all") else outcomes
    for outcome in rows:
        if outcome.outcome_type == EventOutcome.OutcomeType.YES:
            yes = outcome
        elif outcome.outcome_type == EventOutcome.OutcomeType.NO:
            no = outcome
    return yes, no


def put_one_win(price) -> str:
    """Put $1 → Win up to $X."""
    one = format_collateral(Decimal("1"), compact=True)
    amount = _to_decimal(price)
    if amount is None or amount <= 0:
        return f"Put {one} → Win up to —"
    payout = (Decimal("1") / amount).quantize(_TWO, rounding=ROUND_HALF_UP)
    return f"Put {one} → Win up to {format_collateral(payout)}"


def payout_math(pay, price) -> dict[str, Any]:
    """You pay / Maximum possible payout / Potential profit / Maximum loss."""
    stake = _to_decimal(pay) or Decimal("0")
    px = _to_decimal(price)
    if px is None or px <= 0 or stake <= 0:
        return {
            "pay": stake,
            "payout": None,
            "profit": None,
            "loss": stake,
            "pay_label": format_collateral(stake) if stake else "—",
            "payout_label": "—",
            "profit_label": "—",
            "loss_label": format_collateral(stake) if stake else "—",
        }
    payout = (stake / px).quantize(_TWO, rounding=ROUND_HALF_UP)
    profit = (payout - stake).quantize(_TWO, rounding=ROUND_HALF_UP)
    return {
        "pay": stake,
        "payout": payout,
        "profit": profit,
        "loss": stake,
        "pay_label": format_collateral(stake),
        "payout_label": format_collateral(payout),
        "profit_label": format_collateral(profit),
        "loss_label": format_collateral(stake),
    }


def minutes_left(event: EventContract, now=None) -> float:
    expiry = getattr(event, "expiry_time", None)
    if not expiry:
        return 0
    clock = now or timezone.now()
    return (expiry - clock).total_seconds() / 60


def format_ends_in(event: EventContract, now=None) -> str:
    mins = minutes_left(event, now)
    if mins <= 0:
        return "Expired"
    total_seconds = int(mins * 60)
    hours, rem = divmod(total_seconds, 3600)
    minutes, seconds = divmod(rem, 60)
    if hours:
        return f"{hours}h {minutes}m"
    return f"{minutes}:{seconds:02d}"


def _band(score: float) -> str:
    if score >= 75:
        return "High"
    if score >= 55:
        return "Good"
    if score >= 35:
        return "Medium"
    return "Low"


def dreamlens_score(event: EventContract, *, trader_count: int | None = None) -> dict[str, Any]:
    """0–100 analysis signal. Never a probability of winning."""
    trades = int(getattr(event, "trade_count", 0) or 0)
    volume = float(getattr(event, "cumulative_quote_volume", 0) or 0)
    mins = minutes_left(event)
    traders = int(trader_count if trader_count is not None else min(trades, 20))

    activity = min(100.0, trades * 8.0)
    liquidity = min(100.0, volume / 20.0) if volume else min(40.0, trades * 4.0)
    trader_pts = min(100.0, traders * 10.0)
    if mins <= 0:
        time_pts = 0.0
    elif mins <= 15:
        time_pts = 55.0
    elif mins <= 60:
        time_pts = 85.0
    elif mins <= 180:
        time_pts = 70.0
    else:
        time_pts = 45.0

    overall = int(
        round(activity * 0.30 + liquidity * 0.25 + trader_pts * 0.25 + time_pts * 0.20)
    )
    overall = max(0, min(100, overall))
    return {
        "score": overall,
        "activity": _band(activity),
        "liquidity": _band(liquidity),
        "trader_activity": _band(trader_pts),
        "time_remaining": _band(time_pts),
        "disclaimer": SCORE_DISCLAIMER,
    }


def intent_tags(event: EventContract, score: dict[str, Any] | None = None) -> list[str]:
    tags = ["all"]
    types = [str(t).upper() for t in (getattr(event, "radar_types", None) or [])]
    if "MOVING_FAST" in types:
        tags.append("moving-fast")
    if "EXPIRING_SOON" in types:
        tags.append("ending-soon")
    volume = float(getattr(event, "cumulative_quote_volume", 0) or 0)
    trades = int(getattr(event, "trade_count", 0) or 0)
    if volume >= 50 or trades >= 4:
        tags.append("popular")
    scored = score or dreamlens_score(event)
    if int(scored.get("score") or 0) >= 70:
        tags.append("high-score")
    if trades >= 3 or "STRONG_CONSENSUS" in types:
        tags.append("traders-active")
    return tags


def annotate_event_display(event: EventContract, *, trader_count: int | None = None) -> EventContract:
    event.question = event_question(event)  # noqa: SLF001 — template helper
    event.dl_score = dreamlens_score(event, trader_count=trader_count)  # noqa: SLF001
    event.intent_tags = intent_tags(event, event.dl_score)  # noqa: SLF001
    return event


def format_event_card_text(event: EventContract) -> str:
    """Telegram + tests: identical user-facing event block."""
    yes, no = yes_no_outcomes(event)
    yes_px = yes.current_price if yes else None
    no_px = no.current_price if no else None
    return "\n".join(
        [
            event_question(event),
            f"YES {as_cents(yes_px)}",
            f"NO {as_cents(no_px)}",
            f"Ends in {format_ends_in(event)}",
        ]
    )


def format_payout_block(pay, price) -> str:
    math = payout_math(pay, price)
    return "\n".join(
        [
            f"{PAY_LABEL} {math['pay_label']}",
            f"{PAYOUT_LABEL} {math['payout_label']}",
            f"{PROFIT_LABEL} {math['profit_label']}",
            f"{LOSS_LABEL} {math['loss_label']}",
        ]
    )


def asset_mix_groups(assets: list[dict]) -> list[dict[str, Any]]:
    btc = eth = other = 0.0
    for row in assets or []:
        asset = str(row.get("asset") or "").upper()
        share = float(row.get("share") or 0)
        if asset in {"BTC", "BITCOIN"}:
            btc += share
        elif asset in {"ETH", "ETHEREUM"}:
            eth += share
        else:
            other += share
    groups = [
        {"label": "BTC Events", "share": btc},
        {"label": "ETH Events", "share": eth},
        {"label": "Other", "share": other},
    ]
    return [g for g in groups if g["share"] > 0] or groups


def trader_view_signals(*, win_pct, indexed_count, last_fill_at=None) -> dict[str, Any]:
    """How DreamLens sees a trader — analysis signals, not a win guarantee."""
    wr = float(win_pct or 0)
    if wr <= 1:
        wr *= 100
    observed = int(indexed_count or 0)
    sample = min(100, observed)
    activity = min(100, observed * 2)
    if last_fill_at is not None:
        age_hours = (timezone.now() - last_fill_at).total_seconds() / 3600
        if age_hours < 2:
            activity = min(100, activity + 20)
        elif age_hours > 48:
            activity = max(10, activity - 25)
    return {
        "consistency": int(round(min(100, max(0, wr)))),
        "activity": int(round(activity)),
        "sample_size": sample,
        "observed_trades": observed,
    }


def band_from_score(score: int | None) -> str:
    if score is None:
        return "—"
    return _band(float(score))
