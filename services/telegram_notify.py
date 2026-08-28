"""Outbound Telegram messages for DreamAgent events."""

from __future__ import annotations

import logging

from django.db import connection
from django.db.utils import ProgrammingError

from apps.accounts.models import TelegramLink
from apps.agents.models import AgentEvaluation
from services.event_copy import format_collateral
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


def notify_copy_pending(user, execution) -> None:
    """Tell a linked chat that a follow-only Smart Copy is waiting on the web."""
    chat_id = _active_chat_id(user)
    if chat_id is None:
        return
    source = getattr(execution, "source_trade", None)
    event = getattr(source, "event", None)
    title = getattr(event, "title", None) or "DreamDEX event"
    score = getattr(execution, "copy_score", None)
    amount = getattr(execution, "amount", None)
    line = f"Smart Copy waiting: {title}"
    if amount is not None:
        line += f"\n{format_collateral(amount, compact=True)}"
    if score is not None:
        line += f"\nCopy Score: {score}"
    line += "\nConfirm on DreamLens web or ignore."
    try:
        send_message(chat_id, line)
    except TelegramError:
        logger.warning("could not notify pending copy chat_id=%s exec=%s", chat_id, getattr(execution, "pk", None))
