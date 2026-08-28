"""Policy Engine — deterministic agent gates AI cannot mutate."""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

from django.utils import timezone

from apps.agents.models import DreamAgent, DreamAgentPermission
from apps.dreamcopy.models import CopyRelationship, TraderTrade
from services.event_copy import format_collateral


@dataclass
class PolicyContext:
    agent: DreamAgent
    permission: DreamAgentPermission
    source_trade: TraderTrade
    relationship: CopyRelationship | None
    copy_score: int | None
    amount: Decimal
    daily_volume: Decimal = Decimal("0")


@dataclass
class PolicyResult:
    ok: bool
    reasons: list[str] = field(default_factory=list)

    @classmethod
    def pass_(cls) -> PolicyResult:
        return cls(ok=True, reasons=[])

    @classmethod
    def fail(cls, *reasons: str) -> PolicyResult:
        return cls(ok=False, reasons=list(reasons))


class PolicyEngine:
    """Least-authority product rules. Does not modify permissions."""

    def evaluate(self, ctx: PolicyContext) -> PolicyResult:
        reasons: list[str] = []
        agent = ctx.agent
        perm = ctx.permission

        if agent.status != DreamAgent.Status.RUNNING:
            reasons.append(f"Agent is not running (status={agent.status})")

        if not perm.is_valid:
            if perm.status == DreamAgentPermission.Status.REVOKED:
                reasons.append("Delegation revoked")
            elif perm.expires_at <= timezone.now():
                reasons.append("Delegation expired")
            else:
                reasons.append(f"Delegation not active (status={perm.status})")

        if ctx.amount > perm.max_trade_amount:
            reasons.append(
                f"{format_collateral(ctx.amount, compact=True)} exceeds max per trade {format_collateral(perm.max_trade_amount, compact=True)}"
            )

        projected = ctx.daily_volume + ctx.amount
        if projected > perm.max_daily_volume:
            reasons.append(
                f"Daily volume {format_collateral(projected, compact=True)} would exceed {format_collateral(perm.max_daily_volume, compact=True)}"
            )

        min_score = perm.min_copy_score
        if ctx.copy_score is not None and ctx.copy_score < min_score:
            reasons.append(
                f"Copy Score {ctx.copy_score} below your minimum {min_score}"
            )

        allowed = perm.allowed_traders_json or []
        if allowed:
            trader = ctx.source_trade.trader
            allowed_norm = {str(x).lower() for x in allowed}
            ok_trader = (
                str(trader.pk) in allowed_norm
                or trader.wallet_address.lower() in allowed_norm
            )
            if not ok_trader:
                reasons.append("Trader is not in your allowed list")

        outcomes = perm.allowed_outcomes_json or []
        if outcomes:
            ot = ctx.source_trade.outcome.outcome_type
            if ot not in outcomes and ot.upper() not in {o.upper() for o in outcomes}:
                reasons.append(f"Outcome {ot} not allowed")

        event = ctx.source_trade.event
        if event.expiry_time and event.expiry_time <= timezone.now():
            reasons.append("Event is no longer active")

        if reasons:
            return PolicyResult.fail(*reasons)
        return PolicyResult.pass_()


def validate_policy(ctx: PolicyContext) -> PolicyResult:
    return PolicyEngine().evaluate(ctx)
