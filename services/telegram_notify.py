"""Outbound Telegram messages for DreamAgent events."""

from __future__ import annotations

import logging

from django.db import connection
from django.db.utils import ProgrammingError

from apps.accounts.models import TelegramLink
from apps.agents.models import AgentEvaluation
from apps.notifications.models import Notification
from services.event_copy import as_cents, event_question, format_collateral
from integrations.telegram.client import (
    TelegramError,
    explorer_tx_anchor,
    explorer_tx_url,
    html_escape,
    send_html,
)

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


def _h(text) -> str:
    return html_escape(text)


def notify_agent_evaluation(user, ev: AgentEvaluation) -> None:
    policy = ev.policy_json or {}
    if policy.get("source") == "telegram":
        return
    chat_id = _active_chat_id(user)
    if chat_id is None:
        return
    decision = ev.decision
    title = ev.event_title or "Event Contract"
    amount = format_collateral(ev.amount, compact=True) if ev.amount is not None else ""
    side = f" {_h(ev.outcome)}" if ev.outcome else ""
    anchor = explorer_tx_anchor(ev.tx_hash or "")
    if decision == AgentEvaluation.Decision.COPY:
        line = (
            f"<b>DreamAgent copied</b> {_h(amount)}{side} on {_h(title)}."
        )
        if ev.copy_score is not None:
            line += f"\nCopy Score {ev.copy_score}"
        if anchor:
            line += f"\n{anchor}"
    elif decision == AgentEvaluation.Decision.SKIPPED:
        reasons = ev.skip_reasons_json or []
        why = reasons[0] if reasons else "skipped"
        line = f"<b>DreamAgent skipped</b> {_h(title)}: {_h(why)}"
        perm = (policy or {}).get("min_copy_score")
        if perm is not None and ev.copy_score is not None:
            line += f"\nCopy Score {ev.copy_score} (min {perm})"
    else:
        reasons = ev.skip_reasons_json or []
        why = reasons[-1] if reasons else "failed"
        line = f"<b>DreamAgent failed</b> on {_h(title)}: {_h(why)}"
    try:
        send_html(chat_id, line)
    except TelegramError:
        logger.warning("could not notify chat_id=%s evaluation=%s", chat_id, ev.pk)


def _copy_pending_copy(execution) -> tuple[str, str, str]:
    source = getattr(execution, "source_trade", None)
    event = getattr(source, "event", None)
    trader = getattr(getattr(execution, "relationship", None), "trader", None)
    name = ""
    if trader is not None:
        name = (trader.display_name or trader.wallet_address or "").strip()
    question = event_question(event) if event is not None else "Event Contract"
    outcome = getattr(getattr(source, "outcome", None), "outcome_type", "") or ""
    price = getattr(source, "entry_price", None)
    score = getattr(execution, "copy_score", None)
    amount = getattr(execution, "amount", None)
    who = name or "A trader you follow"
    title = f"{who} just traded"
    plain = [title, question]
    html = [f"<b>{html_escape(title)}</b>", html_escape(question)]
    if outcome:
        side = f"{outcome} {as_cents(price)}" if price is not None else outcome
        plain.append(side)
        html.append(html_escape(side))
    if amount is not None:
        stake = format_collateral(amount, compact=True)
        plain.append(stake)
        html.append(html_escape(stake))
    if score is not None:
        plain.append(f"Copy Score {score}")
        html.append(f"Copy Score {html_escape(score)}")
    footer = "DreamAgent did not copy. Confirm on DreamLens or skip."
    plain.append(footer)
    html.append(footer)
    return title, "\n".join(plain), "\n".join(html)


def notify_copy_pending(user, execution) -> None:
    """Alert on Telegram and the web app when a follow is notify-me, not auto-copy."""
    title, plain, html = _copy_pending_copy(execution)
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
            body=plain,
            payload_json=payload,
        )
    except Exception:
        logger.warning("web copy notification failed exec=%s", getattr(execution, "pk", None), exc_info=True)

    chat_id = _active_chat_id(user)
    if chat_id is None:
        logger.info(
            "telegram copy alert skipped — no linked chat user=%s exec=%s",
            getattr(user, "pk", None),
            getattr(execution, "pk", None),
        )
        return
    try:
        send_html(chat_id, html)
    except TelegramError:
        logger.warning("could not notify pending copy chat_id=%s exec=%s", chat_id, getattr(execution, "pk", None))


def notify_auto_claim(
    user,
    *,
    question: str,
    payout: str,
    tx_hash: str = "",
    voided: bool = False,
) -> None:
    """Tell the user DreamAgent redeemed Smart Account winnings."""
    title = "DreamAgent claimed a void" if voided else "DreamAgent claimed a win"
    q = html_escape(question)
    pay = html_escape(payout)
    if voided:
        plain = f"DreamAgent claimed voided collateral ({payout}) on {question}."
        html_body = f"<b>DreamAgent claimed a void</b>\n{pay} collateral on {q}."
    else:
        plain = f"DreamAgent claimed {payout} winnings on {question}."
        html_body = f"<b>DreamAgent claimed a win</b>\n{pay} on {q}."
    anchor = explorer_tx_anchor(tx_hash)
    if tx_hash:
        plain += f"\n{explorer_tx_url(tx_hash)}"
    if anchor:
        html_body += f"\n{anchor}"
    try:
        Notification.objects.create(
            user=user,
            kind="auto_claim",
            title=title,
            body=plain,
            payload_json={"tx_hash": tx_hash, "voided": voided},
        )
    except Exception:
        logger.warning("web auto-claim notification failed user=%s", getattr(user, "pk", None), exc_info=True)
    chat_id = _active_chat_id(user)
    if chat_id is None:
        return
    try:
        send_html(chat_id, html_body)
    except TelegramError:
        logger.warning("could not notify auto-claim chat_id=%s user=%s", chat_id, getattr(user, "pk", None))
