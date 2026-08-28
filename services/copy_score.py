"""Deterministic Copy Score — DreamLens decides; risk engine still gates."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from decimal import Decimal, ROUND_HALF_UP

from django.utils import timezone

from apps.dreamcopy.models import CopyRelationship, TraderProfile, TraderTrade
from apps.events.models import EventContract, EventOutcome, EventRadarSignal
from services.event_copy import format_collateral
from services.consensus_service import compute_consensus

logger = logging.getLogger("dreamlens.services.copy_score")

WEIGHTS = {
    "trader": Decimal("0.25"),
    "event": Decimal("0.20"),
    "market": Decimal("0.20"),
    "consensus": Decimal("0.20"),
    "risk": Decimal("0.15"),
}

DEFAULT_CONSIDER = {
    "trader_confidence": True,
    "historical_performance": True,
    "liquidity": True,
    "market_movement": True,
    "consensus": True,
    "copy_every": False,
}


def _clamp(value: Decimal, lo: Decimal = Decimal("0"), hi: Decimal = Decimal("100")) -> Decimal:
    return max(lo, min(hi, value))


def _pct(score: Decimal) -> int:
    return int(score.quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def _estimate_liquidity(event: EventContract) -> Decimal:
    from services.market_stats import book_liquidity

    return book_liquidity(event)


def _yes_price(event: EventContract) -> Decimal:
    yes = event.outcomes.filter(outcome_type=EventOutcome.OutcomeType.YES).first()
    return yes.current_price if yes else Decimal("0.5")


def _score_trader(trader: TraderProfile) -> tuple[Decimal, list[str], list[str]]:
    why: list[str] = []
    risks: list[str] = []
    win = trader.win_rate or Decimal("0")
    # win_rate may be 0–1 or already percent-ish; treat >1 as percent
    if win > 1:
        win = win / Decimal("100")
    completed = trader.completed_trades or trader.total_trades or 0
    roi = trader.roi or Decimal("0")
    tscore = trader.trader_score or Decimal("0")
    if tscore <= 1:
        tscore = tscore * 100

    win_pts = _clamp(win * 100)
    hist_pts = _clamp(Decimal(min(completed, 200)) / Decimal("2"))  # 100 at 200 events
    roi_pts = _clamp(Decimal("50") + roi)  # 0 ROI → 50
    score_pts = _clamp(tscore)

    score = (
        win_pts * Decimal("0.40")
        + hist_pts * Decimal("0.30")
        + roi_pts * Decimal("0.15")
        + score_pts * Decimal("0.15")
    )

    if completed >= 30:
        why.append(f"{trader.display_name or 'Trader'} has {completed} completed events")
    else:
        risks.append(f"Only {completed} completed events indexed")
    if win >= Decimal("0.65"):
        why.append(f"{_pct(win * 100)}% historical win rate")
    elif win > 0:
        risks.append(f"Win rate {_pct(win * 100)}% is below strong threshold")
    if roi > 0:
        why.append(f"ROI +{roi}%")

    return _clamp(score), why, risks


def _score_event(event: EventContract, outcome: EventOutcome) -> tuple[Decimal, list[str], list[str]]:
    why: list[str] = []
    risks: list[str] = []
    liquidity = _estimate_liquidity(event)
    volume = event.cumulative_quote_volume or Decimal("0")
    mins = float(event.minutes_to_expiry) if hasattr(event, "minutes_to_expiry") else 0
    if event.expiry_time:
        mins = max(0.0, (event.expiry_time - timezone.now()).total_seconds() / 60)

    liq_pts = _clamp(liquidity / Decimal("50"))  # $5k → 100
    vol_pts = _clamp(volume / Decimal("200"))  # $20k → 100
    if mins <= 0:
        time_pts = Decimal("0")
    elif mins >= 60:
        time_pts = Decimal("90")
    else:
        time_pts = _clamp(Decimal(str(mins)) * Decimal("1.5"))

    price = outcome.current_price
    # Prefer mid-range prices (not already extreme)
    distance = abs(price - Decimal("0.5"))
    price_pts = _clamp(Decimal("100") - distance * 150)

    score = (
        liq_pts * Decimal("0.35")
        + vol_pts * Decimal("0.25")
        + time_pts * Decimal("0.25")
        + price_pts * Decimal("0.15")
    )

    if liquidity >= 1000:
        why.append(f"Event has sufficient liquidity ({format_collateral(liquidity, compact=True)})")
    else:
        risks.append(f"Liquidity {format_collateral(liquidity, compact=True)} is thin")
    if mins > 0:
        if mins < 5:
            risks.append(f"Event expires in {mins:.0f} minutes")
        else:
            why.append(f"{mins:.0f} minutes remaining")
    if volume > 0:
        why.append(f"Volume {format_collateral(volume, compact=True)}")

    return _clamp(score), why, risks


def _score_market(event: EventContract, outcome: EventOutcome) -> tuple[Decimal, list[str], list[str]]:
    why: list[str] = []
    risks: list[str] = []
    moving = EventRadarSignal.objects.filter(
        event=event,
        signal_type=EventRadarSignal.SignalType.MOVING_FAST,
        is_active=True,
    ).exists()
    price = outcome.current_price
    # Heuristic momentum: distance from 0.5 with volume
    volume = event.cumulative_quote_volume or Decimal("0")
    mom = abs(price - Decimal("0.5")) * (Decimal("1") + min(volume / Decimal("10000"), Decimal("1")))
    mom_pts = _clamp(mom * 200)
    if moving:
        mom_pts = max(mom_pts, Decimal("75"))
        why.append(f"{outcome.outcome_type} momentum is elevated")
    elif mom_pts >= 50:
        why.append(f"{outcome.outcome_type} price has moved away from fair value")
    else:
        risks.append("Limited recent market movement")

    return _clamp(mom_pts), why, risks


def _score_consensus(
    event: EventContract, outcome: EventOutcome
) -> tuple[Decimal, list[str], list[str], dict]:
    why: list[str] = []
    risks: list[str] = []
    data = compute_consensus(event)
    side = outcome.outcome_type
    favor = data["yes_consensus"] if side == "YES" else data["no_consensus"]
    count = data.get("trader_count") or 0
    score = _clamp(favor * 100)
    if count:
        # Blend in sample size
        sample = min(Decimal(count) / Decimal("8"), Decimal("1"))
        score = _clamp(score * (Decimal("0.7") + sample * Decimal("0.3")))
        pct = _pct(favor * 100)
        if favor >= Decimal("0.6"):
            why.append(f"{pct}% of tracked traders favor {side} ({count} wallets)")
        else:
            risks.append(f"Only {pct}% of tracked traders favor {side}")
    else:
        score = Decimal("50")
        risks.append("No tracked trader consensus yet")
    return score, why, risks, data


def _score_risk(event: EventContract, outcome: EventOutcome, source: TraderTrade) -> tuple[Decimal, list[str], list[str]]:
    why: list[str] = []
    risks: list[str] = []
    mins = 0.0
    if event.expiry_time:
        mins = max(0.0, (event.expiry_time - timezone.now()).total_seconds() / 60)

    # Higher score = safer
    if mins <= 0:
        time_pts = Decimal("0")
        risks.append("Event already expired")
    elif mins < 2:
        time_pts = Decimal("20")
        risks.append(f"Event expires in {mins:.0f} minutes")
    elif mins < 10:
        time_pts = Decimal("55")
        risks.append(f"Short window — expires in {mins:.0f} minutes")
    else:
        time_pts = Decimal("85")
        why.append("Enough time remaining before expiry")

    move = abs(outcome.current_price - source.entry_price)
    if move >= Decimal("0.08"):
        move_pts = Decimal("40")
        risks.append(f"Price has already moved {_pct(move * 100)}% since entry")
    elif move >= Decimal("0.03"):
        move_pts = Decimal("65")
        risks.append(f"Price moved {_pct(move * 100)}% since the trader entered")
    else:
        move_pts = Decimal("90")
        why.append("Entry is still close to current price")

    score = time_pts * Decimal("0.55") + move_pts * Decimal("0.45")
    return _clamp(score), why, risks


@dataclass
class CopyScoreResult:
    decision: str  # COPY | SKIP
    overall: int
    pillars: dict[str, int] = field(default_factory=dict)
    why: list[str] = field(default_factory=list)
    risks: list[str] = field(default_factory=list)
    confidence: Decimal = Decimal("0")
    label: str = "WEAK"
    consensus: dict = field(default_factory=dict)
    liquidity: Decimal = Decimal("0")
    skip_reasons: list[str] = field(default_factory=list)


def score_label(overall: int) -> str:
    if overall >= 80:
        return "STRONG"
    if overall >= 65:
        return "SOLID"
    if overall >= 50:
        return "MIXED"
    return "WEAK"


def evaluate_copy_score(
    *,
    source_trade: TraderTrade,
    relationship: CopyRelationship,
) -> CopyScoreResult:
    """Score a source trade for a copy relationship (advisory until RiskEngine)."""
    event = source_trade.event
    outcome = source_trade.outcome
    trader = relationship.trader
    consider = {**DEFAULT_CONSIDER, **(relationship.consider_json or {})}
    copy_every = bool(consider.get("copy_every"))

    trader_s, tw, tr = _score_trader(trader)
    event_s, ew, er = _score_event(event, outcome)
    market_s, mw, mr = _score_market(event, outcome)
    cons_s, cw, cr, cons_data = _score_consensus(event, outcome)
    risk_s, rw, rr = _score_risk(event, outcome, source_trade)

    # Zero out pillars the user turned off (except when copy_every)
    if not copy_every:
        if not consider.get("trader_confidence", True) and not consider.get(
            "historical_performance", True
        ):
            trader_s = Decimal("70")
        if not consider.get("liquidity", True):
            event_s = Decimal("70")
        if not consider.get("market_movement", True):
            market_s = Decimal("70")
        if not consider.get("consensus", True):
            cons_s = Decimal("70")

    overall_dec = (
        trader_s * WEIGHTS["trader"]
        + event_s * WEIGHTS["event"]
        + market_s * WEIGHTS["market"]
        + cons_s * WEIGHTS["consensus"]
        + risk_s * WEIGHTS["risk"]
    )
    overall = _pct(overall_dec)
    pillars = {
        "trader": _pct(trader_s),
        "event": _pct(event_s),
        "market": _pct(market_s),
        "consensus": _pct(cons_s),
        "risk": _pct(risk_s),
    }

    why = (tw + ew + mw + cw + rw)[:6]
    risks = (tr + er + mr + cr + rr)[:5]
    liquidity = _estimate_liquidity(event)
    skip_reasons: list[str] = []

    decision = "COPY"
    if copy_every:
        decision = "COPY"
        why = why or ["Copy-every preference is on"]
    else:
        min_score = relationship.min_copy_score or 70
        if overall < min_score:
            decision = "SKIP"
            skip_reasons.append(f"Copy Score {overall} below your minimum {min_score}")

        min_wr = relationship.min_win_rate or Decimal("0")
        wr = trader.win_rate or Decimal("0")
        if wr > 1:
            wr = wr / Decimal("100")
        if min_wr and wr < min_wr and consider.get("historical_performance", True):
            decision = "SKIP"
            skip_reasons.append(
                f"Trader win rate {_pct(wr * 100)}% below your minimum {_pct(min_wr * 100)}%"
            )

        min_completed = relationship.min_completed_events or 0
        completed = trader.completed_trades or trader.total_trades or 0
        if min_completed and completed < min_completed and consider.get(
            "historical_performance", True
        ):
            decision = "SKIP"
            skip_reasons.append(
                f"Trader has {completed} events; you require {min_completed}"
            )

        min_liq = relationship.min_liquidity or Decimal("0")
        if min_liq and liquidity < min_liq and consider.get("liquidity", True):
            decision = "SKIP"
            skip_reasons.append(
                f"Event liquidity {format_collateral(liquidity, compact=True)} below your minimum {format_collateral(min_liq, compact=True)}"
            )

        if relationship.copy_mode == CopyRelationship.CopyMode.CONSENSUS or consider.get(
            "consensus", True
        ):
            if relationship.copy_mode == CopyRelationship.CopyMode.CONSENSUS:
                side = outcome.outcome_type
                favor = (
                    cons_data["yes_consensus"]
                    if side == "YES"
                    else cons_data["no_consensus"]
                )
                min_c = relationship.min_consensus or Decimal("0.60")
                if favor < min_c:
                    decision = "SKIP"
                    skip_reasons.append(
                        f"Consensus {_pct(favor * 100)}% below your minimum {_pct(min_c * 100)}%"
                    )

    if decision == "SKIP" and skip_reasons:
        risks = skip_reasons + [r for r in risks if r not in skip_reasons]

    confidence = (Decimal(overall) / Decimal("100")).quantize(
        Decimal("0.0001"), rounding=ROUND_HALF_UP
    )

    return CopyScoreResult(
        decision=decision,
        overall=overall,
        pillars=pillars,
        why=why,
        risks=risks,
        confidence=confidence,
        label=score_label(overall),
        consensus=cons_data,
        liquidity=liquidity,
        skip_reasons=skip_reasons,
    )
