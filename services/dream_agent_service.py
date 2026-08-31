"""DreamAgentService — evaluate → policy → risk → delegated execute."""

from __future__ import annotations

import logging
from decimal import Decimal
from typing import Any

from django.db.models import Count, Q, Sum
from django.utils import timezone

from apps.agents.models import AgentEvaluation, DreamAgent, DreamAgentPermission
from apps.dreamcopy.models import CopyExecution, CopyRelationship, TraderTrade
from apps.trading.models import Trade
from integrations.metamask.delegation import validate_signed_delegation
from integrations.metamask.execution import build_delegated_trade_execution
from integrations.metamask.transactions import broadcast_delegated_execution
from services.copy_score import evaluate_copy_score
from services.copy_service import _copy_amount, _daily_copy_total
from services.event_copy import format_collateral
from services.policy_service import PolicyContext, PolicyEngine
from services.risk_service import RiskContext, RiskEngine
from services.trading_service import confirm_trade, prepare_trade

logger = logging.getLogger("dreamlens.services.dream_agent")


class DreamAgentError(Exception):
    pass


def get_running_agent(user) -> DreamAgent | None:
    """Autonomous copy only. Manual Telegram trades use get_tradable_agent()."""
    return (
        DreamAgent.objects.filter(user=user, status=DreamAgent.Status.RUNNING)
        .select_related("smart_account")
        .order_by("-updated_at")
        .first()
    )


def _agent_with_permission(user, statuses: tuple[str, ...]) -> DreamAgent | None:
    agents = (
        DreamAgent.objects.filter(user=user, status__in=statuses)
        .select_related("smart_account")
        .order_by("-updated_at")
    )
    for agent in agents:
        if active_permission(agent):
            return agent
    return None


def get_tradable_agent(user) -> DreamAgent | None:
    """Agent that can redeem a live TRADE_EVENT_CONTRACT grant (no MetaMask).

    AUTHORIZED means the owner already signed the delegation; RUNNING additionally
    turns on autonomous copy. Telegram is an interface, so both can trade.
    """
    return _agent_with_permission(
        user,
        (DreamAgent.Status.RUNNING, DreamAgent.Status.AUTHORIZED),
    )


def get_session_key_agent(user) -> DreamAgent | None:
    """Grant that can redeemDelegations, including when Smart Copy is paused."""
    return _agent_with_permission(
        user,
        (
            DreamAgent.Status.RUNNING,
            DreamAgent.Status.AUTHORIZED,
            DreamAgent.Status.PAUSED,
        ),
    )


def active_permission(agent: DreamAgent) -> DreamAgentPermission | None:
    perm = (
        DreamAgentPermission.objects.filter(
            agent=agent,
            status=DreamAgentPermission.Status.ACTIVE,
        )
        .order_by("-created_at")
        .first()
    )
    if perm and perm.expires_at <= timezone.now():
        perm.status = DreamAgentPermission.Status.EXPIRED
        perm.save(update_fields=["status", "updated_at"])
        if agent.status == DreamAgent.Status.RUNNING:
            agent.status = DreamAgent.Status.EXPIRED
            agent.save(update_fields=["status", "updated_at"])
        return None
    return perm


def validate_delegation(permission: DreamAgentPermission) -> tuple[bool, list[str]]:
    if not permission.is_valid:
        return False, ["Delegation is not active or has expired"]
    blob = permission.signed_delegation_json or {}
    return validate_signed_delegation(blob)


def grant_health(user) -> dict[str, Any]:
    """What the agent pages should show about the current grant + session key."""
    from integrations.metamask.delegation import GRANT_MISSING_REDEEM, grant_allows_redeem
    from integrations.metamask.transactions import SessionKeyError, get_session_address

    session = ""
    try:
        session = get_session_address()
    except SessionKeyError:
        session = ""
    agent = (
        DreamAgent.objects.filter(user=user)
        .exclude(status=DreamAgent.Status.REVOKED)
        .select_related("smart_account")
        .order_by("-updated_at")
        .first()
    )
    perm = active_permission(agent) if agent else None
    reasons: list[str] = []
    needs_resign = False
    if perm:
        ok, reasons = validate_delegation(perm)
        needs_resign = not ok
        blob = perm.signed_delegation_json or {}
        if not grant_allows_redeem(blob):
            needs_resign = True
            if GRANT_MISSING_REDEEM not in reasons:
                reasons = list(reasons) + [GRANT_MISSING_REDEEM]
    elif agent:
        needs_resign = True
        reasons = ["Sign a DelegationManager grant at /agent/activate/."]
    return {
        "session_address": session,
        "needs_resign": needs_resign,
        "reasons": reasons,
        "has_permission": perm is not None,
    }


def _notify_telegram_evaluation(agent: DreamAgent, ev: AgentEvaluation) -> None:
    try:
        from services.telegram_notify import notify_agent_evaluation

        notify_agent_evaluation(agent.user, ev)
    except Exception:
        logger.exception("telegram notify failed evaluation=%s", ev.pk)


def _record_evaluation(
    *,
    agent: DreamAgent,
    source_trade: TraderTrade | None,
    copy_execution: CopyExecution | None,
    decision: str,
    copy_score: int | None,
    amount: Decimal | None,
    skip_reasons: list[str],
    policy_json: dict,
    risk_json: dict,
    tx_hash: str = "",
    pillars: dict | None = None,
    event_title: str = "",
    outcome: str = "",
) -> AgentEvaluation:
    pillars = pillars or {}
    trader = source_trade.trader if source_trade is not None else None
    ev = AgentEvaluation.objects.create(
        agent=agent,
        source_trade=source_trade,
        copy_execution=copy_execution,
        decision=decision,
        copy_score=copy_score,
        trader_score=pillars.get("trader"),
        event_score=pillars.get("event"),
        consensus_score=pillars.get("consensus"),
        amount=amount,
        skip_reasons_json=skip_reasons,
        policy_json=policy_json,
        risk_json=risk_json,
        tx_hash=tx_hash,
        event_title=(
            event_title
            or (source_trade.event.title if source_trade and source_trade.event_id else "")
        ),
        outcome=(
            outcome
            or (
                source_trade.outcome.outcome_type
                if source_trade and source_trade.outcome_id
                else ""
            )
        ),
        trader_name=(
            (trader.display_name or trader.wallet_address) if trader is not None else "Telegram"
        ),
    )
    _notify_telegram_evaluation(agent, ev)
    return ev


def evaluate_and_maybe_execute(
    source_trade: TraderTrade,
    relationship: CopyRelationship,
    *,
    execution: CopyExecution | None = None,
) -> AgentEvaluation | None:
    """If user has a RUNNING DreamAgent, evaluate and optionally execute autonomously.

    Returns AgentEvaluation when an agent handled the trade; None if no agent.
    """
    agent = get_running_agent(relationship.user)
    if not agent:
        return None

    permission = active_permission(agent)
    if not permission:
        scored = evaluate_copy_score(source_trade=source_trade, relationship=relationship)
        amount = _copy_amount(relationship, source_trade)
        if execution is None:
            execution = CopyExecution.objects.create(
                relationship=relationship,
                source_trade=source_trade,
                ai_decision="SKIP",
                ai_confidence=scored.confidence,
                copy_score=scored.overall,
                score_json=scored.pillars,
                why_json=scored.why,
                risks_json=scored.risks,
                amount=amount,
                status=CopyExecution.Status.SKIPPED,
                reason="No active delegation",
            )
        return _record_evaluation(
            agent=agent,
            source_trade=source_trade,
            copy_execution=execution,
            decision=AgentEvaluation.Decision.SKIPPED,
            copy_score=scored.overall,
            amount=amount,
            skip_reasons=["No active delegation"],
            policy_json={"ok": False},
            risk_json={},
            pillars=scored.pillars,
        )

    return execute_trade(
        agent=agent,
        permission=permission,
        source_trade=source_trade,
        relationship=relationship,
        execution=execution,
    )


def execute_trade(
    *,
    agent: DreamAgent,
    permission: DreamAgentPermission,
    source_trade: TraderTrade,
    relationship: CopyRelationship,
    execution: CopyExecution | None = None,
) -> AgentEvaluation:
    """Full path: score → policy → risk → delegation → prepare → redeem → confirm."""
    amount = (
        min(
            _copy_amount(relationship, source_trade),
            permission.max_trade_amount,
        )
        if relationship.max_per_trade
        else permission.max_trade_amount
    )
    if amount <= 0:
        amount = permission.max_trade_amount

    scored = evaluate_copy_score(source_trade=source_trade, relationship=relationship)
    daily = _daily_copy_total(relationship)

    # Sync relationship limits with permission (permission is source of truth)
    if relationship.max_per_trade != permission.max_trade_amount:
        relationship.max_per_trade = permission.max_trade_amount
    if relationship.max_daily != permission.max_daily_volume:
        relationship.max_daily = permission.max_daily_volume
    if relationship.min_copy_score != permission.min_copy_score:
        relationship.min_copy_score = permission.min_copy_score
    relationship.save(
        update_fields=["max_per_trade", "max_daily", "min_copy_score", "updated_at"]
    )

    policy = PolicyEngine().evaluate(
        PolicyContext(
            agent=agent,
            permission=permission,
            source_trade=source_trade,
            relationship=relationship,
            copy_score=scored.overall,
            amount=amount,
            daily_volume=daily,
        )
    )

    skip_reasons: list[str] = []
    if scored.decision == "SKIP":
        skip_reasons.extend(scored.skip_reasons or ["Copy Score recommended SKIP"])
    if not policy.ok:
        skip_reasons.extend(policy.reasons)

    sa = agent.smart_account
    wallet_address = sa.address

    risk_ok = True
    risk_reasons: list[str] = []
    if not skip_reasons:
        ctx = RiskContext(
            event=source_trade.event,
            outcome=source_trade.outcome,
            amount=amount,
            wallet_address=wallet_address,
            relationship=relationship,
            trader=relationship.trader,
            ai_confidence=scored.confidence,
            ai_decision=scored.decision,
            daily_copy_total=daily,
            copy_score=scored.overall,
            liquidity=scored.liquidity,
            require_wallet=True,
        )
        risk_ok, risk_reasons = RiskEngine().reject(ctx)
        if not risk_ok:
            skip_reasons.extend(risk_reasons)

    del_ok, del_reasons = validate_delegation(permission)
    if not del_ok:
        skip_reasons.extend(del_reasons)

    if execution is None:
        execution = CopyExecution.objects.filter(
            relationship=relationship,
            source_trade=source_trade,
        ).first()

    if skip_reasons:
        if execution is None:
            execution = CopyExecution.objects.create(
                relationship=relationship,
                source_trade=source_trade,
                ai_decision="SKIP",
                ai_confidence=scored.confidence,
                copy_score=scored.overall,
                score_json=scored.pillars,
                why_json=scored.why,
                risks_json=scored.risks,
                amount=amount,
                status=CopyExecution.Status.SKIPPED,
                reason="; ".join(skip_reasons),
            )
        else:
            execution.status = CopyExecution.Status.SKIPPED
            execution.reason = "; ".join(skip_reasons)
            execution.ai_decision = "SKIP"
            execution.ai_confidence = scored.confidence
            execution.copy_score = scored.overall
            execution.score_json = scored.pillars
            execution.why_json = scored.why
            execution.risks_json = scored.risks
            execution.amount = amount
            execution.save()
        return _record_evaluation(
            agent=agent,
            source_trade=source_trade,
            copy_execution=execution,
            decision=AgentEvaluation.Decision.SKIPPED,
            copy_score=scored.overall,
            amount=amount,
            skip_reasons=skip_reasons,
            policy_json={"ok": policy.ok, "reasons": policy.reasons},
            risk_json={"ok": risk_ok, "reasons": risk_reasons},
            pillars=scored.pillars,
        )

    # EXECUTE — AI said COPY and all gates passed
    try:
        trade, unsigned, approval = prepare_trade(
            user=relationship.user,
            event_id=source_trade.event_id,
            outcome=source_trade.outcome.outcome_type,
            amount=amount,
            wallet_address=wallet_address,
            amount_is_notional=True,
        )
        delegated = build_delegated_trade_execution(
            signed_delegation=permission.signed_delegation_json or {},
            dreamdex_tx=unsigned,
            chain_id=sa.chain_id,
            approval_tx=approval,
        )
        tx_hash = broadcast_delegated_execution(
            delegated,
            metadata={
                "agent_id": agent.pk,
                "trade_id": trade.pk,
                "smart_account": sa.address,
            },
        )
        trade = confirm_trade(trade.pk, tx_hash, user=relationship.user)

        if execution:
            execution.copied_trade = trade
            execution.status = CopyExecution.Status.EXECUTED
            execution.amount = amount
            execution.ai_decision = "COPY"
            execution.copy_score = scored.overall
            execution.score_json = scored.pillars
            execution.why_json = scored.why
            execution.risks_json = scored.risks
            execution.reason = "DreamAgent delegated execution"
            execution.save()
        else:
            execution = CopyExecution.objects.create(
                relationship=relationship,
                source_trade=source_trade,
                copied_trade=trade,
                ai_decision="COPY",
                ai_confidence=scored.confidence,
                copy_score=scored.overall,
                score_json=scored.pillars,
                why_json=scored.why,
                risks_json=scored.risks,
                amount=amount,
                status=CopyExecution.Status.EXECUTED,
                reason="DreamAgent delegated execution",
            )

        return _record_evaluation(
            agent=agent,
            source_trade=source_trade,
            copy_execution=execution,
            decision=AgentEvaluation.Decision.COPY,
            copy_score=scored.overall,
            amount=amount,
            skip_reasons=[],
            policy_json={"ok": True},
            risk_json={"ok": True},
            tx_hash=tx_hash,
            pillars=scored.pillars,
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("dream_agent execute failed: %s", exc)
        if execution and execution.status == CopyExecution.Status.PENDING:
            execution.status = CopyExecution.Status.FAILED
            execution.reason = str(exc)
            execution.save(update_fields=["status", "reason"])
        return _record_evaluation(
            agent=agent,
            source_trade=source_trade,
            copy_execution=execution,
            decision=AgentEvaluation.Decision.FAILED,
            copy_score=scored.overall,
            amount=amount,
            skip_reasons=[str(exc)],
            policy_json={"ok": True},
            risk_json={"ok": True},
            pillars=scored.pillars,
        )


def _daily_agent_volume(agent: DreamAgent) -> Decimal:
    today = timezone.now().date()
    total = (
        AgentEvaluation.objects.filter(
            agent=agent,
            decision=AgentEvaluation.Decision.COPY,
            created_at__date=today,
        ).aggregate(total=Sum("amount"))
    )["total"]
    return total or Decimal("0")


def execute_agent_manual_trade(
    user,
    *,
    event_id: int,
    outcome: str,
    amount: Decimal,
    source: str = "telegram",
) -> Trade:
    """Place a DreamDEX order via the user's granted DreamAgent (no MetaMask)."""
    from apps.events.models import EventContract

    origin = (source or "telegram").strip().lower() or "telegram"
    origin_tag = "WEB" if origin == "web" else "TELEGRAM"

    if amount <= 0:
        raise DreamAgentError("Amount must be positive")

    agent = get_tradable_agent(user)
    if not agent:
        if origin == "web":
            raise DreamAgentError(
                "Finish setup at /start/ so DreamLens can place this trade."
            )
        raise DreamAgentError(
            "Activate DreamAgent on /agent/activate/ — Telegram cannot sign MetaMask."
        )
    permission = active_permission(agent)
    if not permission:
        raise DreamAgentError(
            "Your DreamAgent grant expired. Re-sign it on /agent/activate/ in the browser."
        )
    del_ok, del_reasons = validate_delegation(permission)
    if not del_ok:
        raise DreamAgentError("; ".join(del_reasons) or "Delegation is not valid")

    try:
        event = EventContract.objects.prefetch_related("outcomes").get(pk=int(event_id))
    except (EventContract.DoesNotExist, TypeError, ValueError) as exc:
        raise DreamAgentError(f"Event {event_id} not found") from exc

    side = (outcome or "").upper()
    if side not in {"YES", "NO"}:
        raise DreamAgentError("Outcome must be YES or NO")
    outcome_obj = event.outcomes.filter(outcome_type=side).first()
    if not outcome_obj:
        raise DreamAgentError(f"Outcome {side} not found for this event")

    sa = agent.smart_account
    daily = _daily_agent_volume(agent)
    skip_reasons: list[str] = []
    if amount > permission.max_trade_amount:
        skip_reasons.append(
            f"{format_collateral(amount, compact=True)} exceeds max per trade {format_collateral(permission.max_trade_amount, compact=True)}"
        )
    if daily + amount > permission.max_daily_volume:
        skip_reasons.append(
            f"Daily volume would exceed {format_collateral(permission.max_daily_volume, compact=True)}"
        )
    allowed_outcomes = permission.allowed_outcomes_json or []
    if allowed_outcomes and side not in {str(o).upper() for o in allowed_outcomes}:
        skip_reasons.append(f"Outcome {side} not allowed")

    ctx = RiskContext(
        event=event,
        outcome=outcome_obj,
        amount=amount,
        wallet_address=sa.address,
        require_wallet=True,
    )
    risk_ok, risk_reasons = RiskEngine().reject(ctx)
    if not risk_ok:
        skip_reasons.extend(risk_reasons)

    if skip_reasons:
        _record_evaluation(
            agent=agent,
            source_trade=None,
            copy_execution=None,
            decision=AgentEvaluation.Decision.SKIPPED,
            copy_score=None,
            amount=amount,
            skip_reasons=[origin_tag] + skip_reasons,
            policy_json={"ok": False, "source": origin},
            risk_json={"ok": risk_ok, "reasons": risk_reasons},
            event_title=event.title,
            outcome=side,
        )
        raise DreamAgentError("; ".join(skip_reasons))

    try:
        trade, unsigned, approval = prepare_trade(
            user=user,
            event_id=event.pk,
            outcome=side,
            amount=amount,
            wallet_address=sa.address,
            amount_is_notional=True,
        )
        delegated = build_delegated_trade_execution(
            signed_delegation=permission.signed_delegation_json or {},
            dreamdex_tx=unsigned,
            chain_id=sa.chain_id,
            approval_tx=approval,
        )
        tx_hash = broadcast_delegated_execution(
            delegated,
            metadata={
                "agent_id": agent.pk,
                "trade_id": trade.pk,
                "smart_account": sa.address,
                "source": origin,
            },
        )
        trade.metadata_json = {
            **(trade.metadata_json or {}),
            "smart_account": sa.address,
            "source": origin,
        }
        trade.save(update_fields=["metadata_json"])
        trade = confirm_trade(trade.pk, tx_hash, user=user)
        _record_evaluation(
            agent=agent,
            source_trade=None,
            copy_execution=None,
            decision=AgentEvaluation.Decision.COPY,
            copy_score=None,
            amount=amount,
            skip_reasons=[origin_tag],
            policy_json={"ok": True, "source": origin},
            risk_json={"ok": True},
            tx_hash=tx_hash,
            event_title=event.title,
            outcome=side,
        )
        return trade
    except DreamAgentError:
        raise
    except Exception as exc:  # noqa: BLE001
        logger.exception("%s agent trade failed: %s", origin, exc)
        _record_evaluation(
            agent=agent,
            source_trade=None,
            copy_execution=None,
            decision=AgentEvaluation.Decision.FAILED,
            copy_score=None,
            amount=amount,
            skip_reasons=[origin_tag, str(exc)],
            policy_json={"ok": True, "source": origin},
            risk_json={"ok": True},
            event_title=event.title,
            outcome=side,
        )
        raise DreamAgentError(str(exc)) from exc


def agent_performance(agent: DreamAgent) -> dict[str, Any]:
    evals = AgentEvaluation.objects.filter(agent=agent)
    stats = evals.aggregate(
        evaluated=Count("id"),
        copied=Count("id", filter=Q(decision=AgentEvaluation.Decision.COPY)),
        skipped=Count("id", filter=Q(decision=AgentEvaluation.Decision.SKIPPED)),
        failed=Count("id", filter=Q(decision=AgentEvaluation.Decision.FAILED)),
        volume=Sum("amount", filter=Q(decision=AgentEvaluation.Decision.COPY)),
    )
    copied = stats["copied"] or 0
    # Win rate placeholder from copied trades with pnl on linked copy executions
    wins = 0
    if copied:
        for ev in evals.filter(decision=AgentEvaluation.Decision.COPY).select_related(
            "copy_execution__copied_trade"
        ):
            trade = getattr(getattr(ev, "copy_execution", None), "copied_trade", None)
            if trade and getattr(trade, "pnl", None) is not None and trade.pnl > 0:
                wins += 1
    win_rate = (Decimal(wins) / Decimal(copied) * 100) if copied else Decimal("0")

    meta_bal = (agent.smart_account.metadata_json or {}).get("balance")
    current = Decimal(str(meta_bal or agent.initial_capital or 0))
    initial = agent.initial_capital or Decimal("0")
    pnl = current - initial if initial else Decimal("0")
    roi = (pnl / initial * 100) if initial else Decimal("0")

    perm = active_permission(agent)
    return {
        "agent": {
            "id": agent.pk,
            "name": agent.name,
            "status": agent.status,
            "session_address": agent.session_address,
        },
        "smart_account": {
            "id": agent.smart_account_id,
            "address": agent.smart_account.address,
            "status": agent.smart_account.status,
        },
        "balance": str(current),
        "initial_capital": str(initial),
        "pnl": str(pnl),
        "roi": str(roi.quantize(Decimal("0.1"))),
        "trades_evaluated": stats["evaluated"] or 0,
        "trades_copied": copied,
        "trades_skipped": stats["skipped"] or 0,
        "trades_failed": stats["failed"] or 0,
        "win_rate": str(win_rate.quantize(Decimal("0.1"))),
        "volume_copied": str(stats["volume"] or 0),
        "permission": (
            {
                "id": perm.pk,
                "max_trade_amount": str(perm.max_trade_amount),
                "max_daily_volume": str(perm.max_daily_volume),
                "min_copy_score": perm.min_copy_score,
                "expires_at": perm.expires_at.isoformat(),
                "status": perm.status,
                "allowed_traders": perm.allowed_traders_json,
            }
            if perm
            else None
        ),
        "autonomous": agent.status == DreamAgent.Status.RUNNING,
    }


def serialize_evaluation(ev: AgentEvaluation) -> dict[str, Any]:
    return {
        "id": ev.pk,
        "decision": ev.decision,
        "copy_score": ev.copy_score,
        "trader_score": str(ev.trader_score) if ev.trader_score is not None else None,
        "event_score": str(ev.event_score) if ev.event_score is not None else None,
        "consensus_score": (
            str(ev.consensus_score) if ev.consensus_score is not None else None
        ),
        "amount": str(ev.amount) if ev.amount is not None else None,
        "skip_reasons": ev.skip_reasons_json or [],
        "tx_hash": ev.tx_hash,
        "event_title": ev.event_title,
        "outcome": ev.outcome,
        "trader_name": ev.trader_name,
        "created_at": ev.created_at.isoformat() if ev.created_at else None,
        "policy": ev.policy_json,
        "risk": ev.risk_json,
    }
