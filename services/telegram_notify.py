"""Outbound Telegram messages for DreamAgent events."""

from __future__ import annotations

import logging

from django.db import connection
from django.db.utils import ProgrammingError

from apps.accounts.models import TelegramLink
from apps.agents.models import AgentEvaluation
from apps.notifications.models import Notification
from services.event_copy import as_cents, event_question, format_collateral
from integrations.telegram.client import TelegramError, explorer_tx_url, send_message

logger = logging.getLogger("dreamlens.telegram.notify")


def _active_chat_id(user) -> int | None:
    try:
        link = TelegramLink.objects.filter(
            user=user, status=TelegramLink.Status.ACTIVE
        ).first()
    except ProgrammingError:
        logger.warning("TelegramLink table missing; skip notify")
        try:
            connection.rollback()
        except Exception:
            pass
        return None
    if not link:
        return None
    return int(link.chat_id)


def notify_agent_evaluation(user, ev: AgentEvaluation) -> None:
    policy = ev.policy_json or {}
    if policy.get("source") == "telegram":
        return
    chat_id = _active_chat_id(user)
    if chat_id is None:
        return
    decision = ev.decision
    title = ev.event_title or "DreamDEX event"
    amount = format_collateral(ev.amount, compact=True) if ev.amount is not None else ""
    side = f" {ev.outcome}" if ev.outcome else ""
    if decision == AgentEvaluation.Decision.COPY:
        line = f"DreamAgent copied {amount}{side} on {title}."
        if ev.copy_score is not None:
            line += f"\nCopy Score: {ev.copy_score}"
        if ev.tx_hash:
            line += f"\n{explorer_tx_url(ev.tx_hash)}"
    elif decision == AgentEvaluation.Decision.SKIPPED:
        reasons = ev.skip_reasons_json or []
        why = reasons[0] if reasons else "skipped"
        line = f"DreamAgent skipped {title}: {why}"
        perm = (policy or {}).get("min_copy_score")
        if perm is not None and ev.copy_score is not None:
            line += f"\nCopy Score {ev.copy_score} (min {perm})"
    else:
        reasons = ev.skip_reasons_json or []
        why = reasons[-1] if reasons else "failed"
        line = f"DreamAgent failed on {title}: {why}"
    try:
        send_message(chat_id, line)
    except TelegramError:
        logger.warning("could not notify chat_id=%s evaluation=%s", chat_id, ev.pk)


def _copy_pending_copy(execution) -> tuple[str, str]:
    source = getattr(execution, "source_trade", None)
    event = getattr(source, "event", None)
    trader = getattr(getattr(execution, "relationship", None), "trader", None)
    name = ""
    if trader is not None:
        name = (trader.display_name or trader.wallet_address or "").strip()
    question = event_question(event) if event is not None else "DreamDEX event"
    outcome = getattr(getattr(source, "outcome", None), "outcome_type", "") or ""
    price = getattr(source, "entry_price", None)
    score = getattr(execution, "copy_score", None)
    amount = getattr(execution, "amount", None)
    who = name or "A trader you follow"
    title = f"{who} just traded"
    lines = [title, question]
    if outcome:
        lines.append(f"{outcome} {as_cents(price)}" if price is not None else outcome)
    if amount is not None:
        lines.append(format_collateral(amount, compact=True))
    if score is not None:
        lines.append(f"Copy Score: {score}")
    lines.append("DreamAgent did not copy. Confirm on DreamLens or skip.")
    return title, "\n".join(lines)


def notify_copy_pending(user, execution) -> None:
    """Alert on Telegram and the web app when a follow is notify-me, not auto-copy."""
    title, body = _copy_pending_copy(execution)
    source = getattr(execution, "source_trade", None)
    event = getattr(source, "event", None)
    trader = getattr(getattr(execution, "relationship", None), "trader", None)
    payload = {
        "execution_id": getattr(execution, "pk", None),
        "event_id": getattr(event, "pk", None),
        "trader_id": getattr(trader, "pk", None),
    }
    try:
        Notification.objects.create(
            user=user,
            kind="copy_pending",
            title=title,
            body=body,
            payload_json=payload,
        )
    except Exception:
        logger.warning("web copy notification failed exec=%s", getattr(execution, "pk", None), exc_info=True)

    chat_id = _active_chat_id(user)
    if chat_id is None:
        return
    try:
        send_message(chat_id, body)
    except TelegramError:
        logger.warning("could not notify pending copy chat_id=%s exec=%s", chat_id, getattr(execution, "pk", None))
