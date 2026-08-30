"""DreamCopy relationship management and Smart Copy execution."""

from __future__ import annotations

import logging
from decimal import Decimal

from django.db import IntegrityError, transaction
from django.db.models import Sum
from django.utils import timezone

from apps.dreamcopy.models import CopyExecution, CopyRelationship, TraderProfile, TraderTrade
from apps.trading.models import Trade
from integrations.dreamdex.types import UnsignedTxDTO
from services.copy_score import evaluate_copy_score
from services.risk_service import RiskContext, RiskEngine
from services.trading_service import prepare_trade

logger = logging.getLogger("dreamlens.services.copy")


class CopyError(Exception):
    pass


DEFAULT_CONSIDER = {
    "trader_confidence": True,
    "historical_performance": True,
    "liquidity": True,
    "market_movement": True,
    "consensus": True,
    "copy_every": False,
}


def _daily_copy_total(relationship: CopyRelationship) -> Decimal:
    today = timezone.now().date()
    total = (
        CopyExecution.objects.filter(
            relationship=relationship,
            created_at__date=today,
            status__in=[
                CopyExecution.Status.PENDING,
                CopyExecution.Status.APPROVED,
                CopyExecution.Status.EXECUTED,
            ],
        ).aggregate(total=Sum("amount"))
    )["total"]
    return total or Decimal("0")


def _copy_amount(relationship: CopyRelationship, source_trade: TraderTrade) -> Decimal:
    amount = relationship.max_per_trade or Decimal("10")
    if amount <= 0:
        amount = Decimal("10")
    return amount


@transaction.atomic
def create_copy_relationship(user, data: dict) -> CopyRelationship:
    from services.trader_service import ensure_trader_profile, normalize_trader_wallet

    trader_id = data.get("trader_id")
    wallet = (data.get("wallet_address") or "").strip()
    if wallet and not trader_id:
        try:
            addr = normalize_trader_wallet(wallet)
        except ValueError as exc:
            raise CopyError(str(exc)) from exc
        if user.wallets.filter(address__iexact=addr).exists():
            raise CopyError("You cannot follow your own wallet.")
        trader_id = ensure_trader_profile(addr).pk
    if not trader_id:
        raise CopyError("Pick a trader or paste a wallet address.")
    if not TraderProfile.objects.filter(pk=trader_id).exists():
        raise CopyError("Trader not found.")

    consider = data.get("consider_json") or {}
    if not isinstance(consider, dict):
        consider = {}
    consider = {**DEFAULT_CONSIDER, **consider}

    defaults = {
        "status": data.get("status", CopyRelationship.Status.ACTIVE),
        "copy_mode": data.get("copy_mode", CopyRelationship.CopyMode.SMART),
        "max_per_trade": data.get("max_per_trade", Decimal("10")),
        "max_daily": data.get("max_daily", Decimal("50")),
        "minimum_confidence": data.get("minimum_confidence"),
        "min_copy_score": data.get("min_copy_score", 70),
        "min_win_rate": data.get("min_win_rate", Decimal("0.65")),
        "min_completed_events": data.get("min_completed_events", 30),
        "min_liquidity": data.get("min_liquidity", Decimal("1000")),
        "min_consensus": data.get("min_consensus", Decimal("0.60")),
        "consider_json": consider,
        "allowed_assets_json": data.get("allowed_assets_json", []),
        "auto_execute": data.get("auto_execute", False),
    }
    rel, _ = CopyRelationship.objects.update_or_create(
        user=user,
        trader_id=trader_id,
        defaults=defaults,
    )
    _include_trader_on_agent_allowlist(user, rel.trader)
    return rel


@transaction.atomic
def update_copy_relationship(relationship: CopyRelationship, data: dict) -> CopyRelationship:
    for field in (
        "status",
        "copy_mode",
        "max_per_trade",
        "max_daily",
        "minimum_confidence",
        "min_copy_score",
        "min_win_rate",
        "min_completed_events",
        "min_liquidity",
        "min_consensus",
        "consider_json",
        "allowed_assets_json",
        "auto_execute",
    ):
        if field in data:
            setattr(relationship, field, data[field])
    relationship.save()
    if relationship.status == CopyRelationship.Status.ACTIVE:
        _include_trader_on_agent_allowlist(relationship.user, relationship.trader)
    return relationship


@transaction.atomic
def delete_copy_relationship(relationship: CopyRelationship) -> None:
    relationship.status = CopyRelationship.Status.STOPPED
    relationship.save(update_fields=["status", "updated_at"])


def _include_trader_on_agent_allowlist(user, trader) -> None:
    """A later /follow must be copyable even if the grant snapshotted an older list."""
    if trader is None:
        return
    from apps.agents.models import DreamAgentPermission

    tid = str(trader.pk)
    wallet = (getattr(trader, "wallet_address", "") or "").lower()
    perms = DreamAgentPermission.objects.filter(
        agent__user=user,
        status=DreamAgentPermission.Status.ACTIVE,
    )
    for perm in perms:
        allowed = list(perm.allowed_traders_json or [])
        if not allowed:
            continue
        normalized = {str(x).lower() for x in allowed}
        if tid.lower() in normalized or (wallet and wallet in normalized):
            continue
        allowed.append(tid)
        perm.allowed_traders_json = allowed
        perm.save(update_fields=["allowed_traders_json", "updated_at"])


def detect_and_process_copy(source_trade: TraderTrade) -> list[CopyExecution]:
    """Process new trader trade: Copy Score → rules → RiskEngine → PENDING/SKIPPED.

    If the user has a RUNNING DreamAgent with active delegation, the agent path
    (Policy → Risk → delegated Smart Account execution) owns the outcome instead
    of leaving a user-signature PENDING row.

    On-chain redeem must not sit inside a Django atomic block — the session
    pooler cannot hold a transaction open for a live `redeemDelegations`.
    """
    from services.dream_agent_service import (
        evaluate_and_maybe_execute,
        get_running_agent,
    )

    relationships = CopyRelationship.objects.filter(
        trader=source_trade.trader,
        status=CopyRelationship.Status.ACTIVE,
    ).select_related("user", "trader")

    executions: list[CopyExecution] = []
    for rel in relationships:
        if CopyExecution.objects.filter(
            relationship=rel,
            source_trade=source_trade,
        ).exists():
            continue

        # Immediate copy only when the user opted in *and* DreamAgent is RUNNING.
        # Notify-me follows stay on the review path even if the agent is active.
        if rel.auto_execute and get_running_agent(rel.user):
            try:
                evaluation = evaluate_and_maybe_execute(source_trade, rel)
            except IntegrityError:
                logger.info(
                    "agent_copy already recorded rel=%s trade=%s",
                    rel.pk,
                    source_trade.pk,
                )
                continue
            if evaluation and evaluation.copy_execution_id:
                executions.append(evaluation.copy_execution)
                logger.info(
                    "agent_copy rel=%s trade=%s decision=%s score=%s",
                    rel.pk,
                    source_trade.pk,
                    evaluation.decision,
                    evaluation.copy_score,
                )
            continue

        amount = _copy_amount(rel, source_trade)
        scored = evaluate_copy_score(source_trade=source_trade, relationship=rel)

        ai_decision = scored.decision
        ai_confidence = scored.confidence
        reason = ""
        status = CopyExecution.Status.PENDING

        if scored.decision == "SKIP":
            status = CopyExecution.Status.SKIPPED
            reason = "; ".join(scored.skip_reasons) or "Copy Score below threshold"
        else:
            wallet = rel.user.wallets.filter(is_primary=True).first()
            wallet_address = wallet.address if wallet else ""

            # REVIEW mode: allow missing wallet at score time; require it at prepare.
            # Risk still checks limits / event / relationship.
            ctx = RiskContext(
                event=source_trade.event,
                outcome=source_trade.outcome,
                amount=amount,
                wallet_address=wallet_address or "pending",
                relationship=rel,
                trader=rel.trader,
                ai_confidence=ai_confidence,
                ai_decision=ai_decision,
                daily_copy_total=_daily_copy_total(rel),
                copy_score=scored.overall,
                liquidity=scored.liquidity,
                require_wallet=False,
            )
            ok, reasons = RiskEngine().reject(ctx)
            if not ok:
                status = CopyExecution.Status.SKIPPED
                reason = "; ".join(reasons)
                ai_decision = "SKIP"
            else:
                status = CopyExecution.Status.PENDING
                reason = (
                    "DreamAgent is not Active — confirm this copy, or start the agent."
                    if rel.auto_execute
                    else "Notify me — confirm on DreamLens or skip."
                )

        try:
            execution = CopyExecution.objects.create(
                relationship=rel,
                source_trade=source_trade,
                ai_decision=ai_decision,
                ai_confidence=ai_confidence,
                copy_score=scored.overall,
                score_json=scored.pillars,
                why_json=scored.why,
                risks_json=scored.risks,
                amount=amount,
                status=status,
                reason=reason,
            )
        except IntegrityError:
            continue
        executions.append(execution)
        logger.info(
            "copy_exec rel=%s trade=%s status=%s score=%s",
            rel.pk,
            source_trade.pk,
            status,
            scored.overall,
        )
        if status == CopyExecution.Status.PENDING:
            try:
                from services.telegram_notify import notify_copy_pending

                notify_copy_pending(rel.user, execution)
            except Exception:
                logger.warning("copy pending notify failed", exc_info=True)

    return executions


@transaction.atomic
def skip_copy_execution(execution_id: int, *, user) -> CopyExecution:
    try:
        execution = (
            CopyExecution.objects.select_for_update()
            .select_related("relationship")
            .get(pk=execution_id, relationship__user=user)
        )
    except CopyExecution.DoesNotExist as exc:
        raise CopyError(f"Copy execution {execution_id} not found") from exc

    if execution.status != CopyExecution.Status.PENDING:
        raise CopyError(f"Execution not skippable (status={execution.status})")

    execution.status = CopyExecution.Status.SKIPPED
    execution.reason = execution.reason or "Skipped by user"
    if "Skipped by user" not in execution.reason:
        execution.reason = f"{execution.reason}; Skipped by user".strip("; ")
    execution.save(update_fields=["status", "reason"])
    return execution


@transaction.atomic
def prepare_copy_trade(
    execution_id: int, *, user, wallet_address: str
) -> tuple[CopyExecution, Trade, UnsignedTxDTO, object | None]:
    """User confirms a pending copy execution → prepare underlying trade."""
    try:
        execution = (
            CopyExecution.objects.select_for_update()
            .select_related(
                "relationship",
                "relationship__user",
                "relationship__trader",
                "source_trade",
                "source_trade__event",
                "source_trade__outcome",
            )
            .get(pk=execution_id, relationship__user=user)
        )
    except CopyExecution.DoesNotExist as exc:
        raise CopyError(f"Copy execution {execution_id} not found") from exc

    if execution.status not in (
        CopyExecution.Status.PENDING,
        CopyExecution.Status.APPROVED,
    ):
        raise CopyError(f"Execution not confirmable (status={execution.status})")

    source = execution.source_trade
    rel = execution.relationship
    amount = execution.amount or _copy_amount(rel, source)

    ctx = RiskContext(
        event=source.event,
        outcome=source.outcome,
        amount=amount,
        wallet_address=wallet_address,
        relationship=rel,
        trader=rel.trader,
        ai_confidence=execution.ai_confidence,
        ai_decision=execution.ai_decision or "COPY",
        daily_copy_total=_daily_copy_total(rel),
        copy_score=execution.copy_score,
        require_wallet=True,
    )
    ok, reasons = RiskEngine().reject(ctx)
    if not ok:
        execution.status = CopyExecution.Status.SKIPPED
        execution.reason = "; ".join(reasons)
        execution.save(update_fields=["status", "reason"])
        raise CopyError(execution.reason)

    trade, unsigned, approval = prepare_trade(
        user=user,
        event_id=source.event_id,
        outcome=source.outcome.outcome_type,
        amount=amount,
        wallet_address=wallet_address,
        amount_is_notional=True,
    )
    execution.copied_trade = trade
    execution.status = CopyExecution.Status.APPROVED
    execution.reason = "Trade prepared — awaiting on-chain confirmation"
    execution.save(update_fields=["copied_trade", "status", "reason"])

    return execution, trade, unsigned, approval


def serialize_execution(execution: CopyExecution) -> dict:
    """API-friendly dict for pending / activity / alert UI."""
    source = execution.source_trade
    event = source.event
    outcome = source.outcome
    trader = execution.relationship.trader
    amount = execution.amount or execution.relationship.max_per_trade or Decimal("10")
    pillars = execution.score_json or {}
    return {
        "id": execution.pk,
        "status": execution.status,
        "ai_decision": execution.ai_decision,
        "copy_score": execution.copy_score,
        "score_label": (
            "STRONG"
            if (execution.copy_score or 0) >= 80
            else "SOLID"
            if (execution.copy_score or 0) >= 65
            else "MIXED"
            if (execution.copy_score or 0) >= 50
            else "WEAK"
        ),
        "score_json": pillars,
        "why_json": execution.why_json or [],
        "risks_json": execution.risks_json or [],
        "reason": execution.reason,
        "amount": str(amount),
        "created_at": execution.created_at.isoformat() if execution.created_at else None,
        "trader": {
            "id": trader.pk,
            "wallet_address": trader.wallet_address,
            "display_name": trader.display_name or trader.wallet_address,
            "win_rate": str(trader.win_rate),
            "roi": str(trader.roi),
            "completed_trades": trader.completed_trades or trader.total_trades,
        },
        "event": {
            "id": event.pk,
            "title": event.title,
            "underlying_asset": event.underlying_asset,
            "expiry_time": event.expiry_time.isoformat() if event.expiry_time else None,
        },
        "outcome": outcome.outcome_type,
        "entry_price": str(source.entry_price),
        "relationship_id": execution.relationship_id,
        "copied_trade_id": execution.copied_trade_id,
    }
