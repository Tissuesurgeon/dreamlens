"""Deterministic risk checks — Copy Score / AI never override these."""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

from django.utils import timezone

from apps.dreamcopy.models import CopyRelationship, TraderProfile
from apps.events.models import EventContract, EventOutcome

MIN_TRADER_COMPLETED_TRADES = 5
MIN_TRADER_SCORE = Decimal("0.35")
ACTIVE_STATUSES = {
    EventContract.Status.TRADING,
    EventContract.Status.LIVE,
    EventContract.Status.LISTED,
}


@dataclass
class RiskContext:
    """Inputs for a copy or direct trade risk evaluation."""

    event: EventContract
    outcome: EventOutcome
    amount: Decimal
    wallet_address: str | None = None
    relationship: CopyRelationship | None = None
    trader: TraderProfile | None = None
    ai_confidence: Decimal | None = None
    ai_decision: str | None = None
    daily_copy_total: Decimal = Decimal("0")
    copy_score: int | None = None
    liquidity: Decimal | None = None
    require_wallet: bool = True


@dataclass
class RiskResult:
    ok: bool
    reasons: list[str] = field(default_factory=list)

    @classmethod
    def pass_(cls) -> RiskResult:
        return cls(ok=True, reasons=[])

    @classmethod
    def fail(cls, *reasons: str) -> RiskResult:
        return cls(ok=False, reasons=list(reasons))


class RiskEngine:
    """Deterministic risk gate. AI / Copy Score are advisory only."""

    def reject(self, ctx: RiskContext) -> tuple[bool, list[str]]:
        result = self.evaluate(ctx)
        return result.ok, result.reasons

    def evaluate(self, ctx: RiskContext) -> RiskResult:
        reasons: list[str] = []

        if ctx.event.status not in ACTIVE_STATUSES:
            reasons.append(f"Event is not active (status={ctx.event.status})")

        if ctx.event.expiry_time <= timezone.now():
            reasons.append("Event has expired")

        if ctx.outcome.event_id != ctx.event.pk:
            reasons.append("Outcome does not belong to event")

        if ctx.require_wallet and (
            not ctx.wallet_address or ctx.wallet_address == "pending"
        ):
            reasons.append("Wallet not connected")

        rel = ctx.relationship
        if rel is not None:
            if rel.status != CopyRelationship.Status.ACTIVE:
                reasons.append(f"Copy relationship is {rel.status}")

            max_per_trade = rel.max_per_trade
            if max_per_trade is not None and ctx.amount > max_per_trade:
                reasons.append(
                    f"Amount {ctx.amount} exceeds max_per_trade {max_per_trade}"
                )

            if rel.max_daily is not None:
                projected = ctx.daily_copy_total + ctx.amount
                if projected > rel.max_daily:
                    reasons.append(
                        f"Daily copy limit exceeded ({projected} > {rel.max_daily})"
                    )

            if rel.allowed_assets_json:
                asset = ctx.event.underlying_asset.upper()
                allowed = {a.upper() for a in rel.allowed_assets_json}
                if asset not in allowed:
                    reasons.append(f"Asset {asset} not allowed for this copy rule")

            # Prefer Copy Score threshold when present; fall back to legacy confidence.
            if rel.copy_mode == CopyRelationship.CopyMode.SMART:
                if ctx.copy_score is not None:
                    min_score = rel.min_copy_score or 70
                    if ctx.copy_score < min_score:
                        reasons.append(
                            f"Copy Score {ctx.copy_score} below minimum {min_score}"
                        )
                else:
                    min_conf = rel.minimum_confidence or Decimal("0.55")
                    if ctx.ai_confidence is None:
                        reasons.append("SMART copy requires a confidence score")
                    elif ctx.ai_confidence < min_conf:
                        reasons.append(
                            f"Confidence {ctx.ai_confidence} below threshold {min_conf}"
                        )
                if ctx.ai_decision and ctx.ai_decision.upper() == "SKIP":
                    reasons.append("Smart Copy recommended SKIP")

            min_wr = getattr(rel, "min_win_rate", None)
            trader = ctx.trader or rel.trader
            if min_wr is not None and trader is not None:
                wr = trader.win_rate or Decimal("0")
                if wr > 1:
                    wr = wr / Decimal("100")
                consider = rel.consider_json or {}
                if consider.get("historical_performance", True) and wr < min_wr:
                    reasons.append(
                        f"Trader win rate {wr} below your minimum {min_wr}"
                    )

            min_completed = getattr(rel, "min_completed_events", None) or 0
            if min_completed and trader is not None:
                completed = trader.completed_trades or trader.total_trades or 0
                consider = rel.consider_json or {}
                if consider.get("historical_performance", True) and completed < min_completed:
                    reasons.append(
                        f"Trader has {completed} events; minimum is {min_completed}"
                    )

            min_liq = getattr(rel, "min_liquidity", None)
            if min_liq is not None and ctx.liquidity is not None:
                consider = rel.consider_json or {}
                if consider.get("liquidity", True) and ctx.liquidity < min_liq:
                    reasons.append(
                        f"Liquidity {ctx.liquidity} below your minimum {min_liq}"
                    )

        trader = ctx.trader or (rel.trader if rel else None)
        if trader is not None and rel is None:
            # Direct trade path: keep legacy floor
            if trader.completed_trades < MIN_TRADER_COMPLETED_TRADES:
                reasons.append(
                    f"Trader has insufficient history ({trader.completed_trades} completed)"
                )
            if trader.trader_score < MIN_TRADER_SCORE:
                reasons.append(
                    f"Trader score {trader.trader_score} below minimum {MIN_TRADER_SCORE}"
                )

        if reasons:
            return RiskResult(ok=False, reasons=reasons)
        return RiskResult.pass_()


def reject_copy_trade(ctx: RiskContext) -> tuple[bool, list[str]]:
    """Convenience wrapper for copy flows."""
    return RiskEngine().reject(ctx)
