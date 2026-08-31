"""Telegram bot command handler for DreamAgent."""

from __future__ import annotations

import logging
import re
from decimal import Decimal, InvalidOperation
from typing import Any

from django.core.cache import cache
from django.utils import timezone

from apps.dreamcopy.models import CopyRelationship
from apps.events.models import EventContract
from integrations.telegram.client import (
    answer_callback,
    bot_url,
    explorer_tx_anchor,
    html_escape,
    html_link,
    inline_keyboard,
    send_html,
)
from services.ai_service import analyze_event, parse_intent
from services.event_copy import (
    SCORE_DISCLAIMER,
    as_cents,
    event_question,
    format_collateral,
    format_event_card_text,
    format_payout_block,
    format_window_line,
    watching_brief,
    yes_no_outcomes,
)
from services.copy_service import CopyError, create_copy_relationship
from services.dream_agent_service import (
    DreamAgentError,
    active_permission,
    agent_performance,
    execute_agent_manual_trade,
    get_running_agent,
    get_tradable_agent,
)
from services.telegram_link_service import (
    TelegramLinkError,
    active_link_for_chat,
    cancel_pending_link,
    confirm_link,
    site_origin,
)
from services.trader_service import list_active_traders

logger = logging.getLogger("dreamlens.telegram.bot")

HELP = (
    "<b>DreamLens</b> — DreamAgent on Somnia.\n"
    "This chat is an interface. Trades and claims use your Smart Account.\n\n"
    "<b>Look</b>\n"
    "/events — live Event Contracts\n"
    "/analyze 12 — reading on a window\n\n"
    "<b>Trade</b>\n"
    "/trade 12 YES 5 — DreamAgent buys, no MetaMask\n"
    "Or say: Buy $5 YES on BTC\n\n"
    "<b>You</b>\n"
    "/agent · /positions · /balance\n"
    "/pause · /resume — Smart Copy\n\n"
    "<b>Copy</b>\n"
    "/traders · /follow · /copy · /following"
)

_STATUS_LABEL = {
    "RUNNING": "Active",
    "PAUSED": "Paused",
    "AUTHORIZED": "Ready — copy off",
    "EXPIRED": "Grant expired",
    "REVOKED": "Revoked",
    "CONFIGURED": "Needs grant",
    "FUNDED": "Needs grant",
    "CREATED": "Needs grant",
}

_RESULT_LABEL = {
    "open": "Open",
    "won": "Won",
    "void": "Void",
    "lost": "Lost",
    "settling": "Settling",
    "claimed": "Claimed",
    "closed": "Closed",
}

_DECISION_LABEL = {
    "COPY": "Copied",
    "SKIPPED": "Skipped",
    "FAILED": "Failed",
}

TRADE_TTL = 300
_TG_TRADES_KEY = "tg_trades"
_TRADE_RE = re.compile(
    r"^/trade(?:@\w+)?\s+(\d+)\s+(YES|NO)\s+([0-9]+(?:\.[0-9]+)?)\s*$",
    re.IGNORECASE,
)
_ANALYZE_RE = re.compile(
    r"^/analyze(?:@\w+)?(?:\s+(\d+|[A-Za-z]+))?\s*$",
    re.IGNORECASE,
)
_PAUSE_PHRASES = ("pause my agent", "pause agent", "pause the agent")
_RESUME_PHRASES = ("resume my agent", "resume agent", "unpause", "start my agent")
_POSITION_PHRASES = ("show positions", "my positions", "portfolio", "my portfolio")
_AGENT_PHRASES = ("how is my agent", "agent status", "my agent")

LIMIT_TEMPLATE = (
    "Your DreamAgent limit is {max} per trade. I cannot execute {asked}. "
    "Change limits on the Agent page with MetaMask."
)


def _h(text) -> str:
    return html_escape(text)


def _send(chat_id: int, text: str, *, reply_markup: dict | None = None) -> None:
    send_html(chat_id, text, reply_markup=reply_markup)


def _activate_url() -> str:
    return f"{site_origin()}/agent/activate/"


def _portfolio_url() -> str:
    return f"{site_origin()}/portfolio/"


def _nav_keyboard():
    return inline_keyboard(
        [
            [("Events", "go:events"), ("Positions", "go:pos")],
            [("Agent", "go:agent"), ("Traders", "go:traders")],
            [("Help", "go:help")],
        ]
    )


def _agent_keyboard(agent) -> dict:
    from apps.agents.models import DreamAgent

    rows: list[list[tuple[str, str]]] = []
    if agent and agent.status == DreamAgent.Status.RUNNING:
        rows.append([("Pause copy", "go:pause")])
    elif agent and agent.status == DreamAgent.Status.PAUSED:
        rows.append([("Resume copy", "go:resume")])
    rows.append([("Positions", "go:pos"), ("Activity", "go:activity")])
    rows.append([("Events", "go:events")])
    return inline_keyboard(rows)


def handle_update(update: dict[str, Any]) -> None:
    """Process one Telegram update. Safe to call from webhook or poll."""
    try:
        if update.get("callback_query"):
            _handle_callback(update["callback_query"])
            return
        message = update.get("message") or update.get("edited_message")
        if not message:
            return
        text = (message.get("text") or "").strip()
        chat = message.get("chat") or {}
        chat_id = chat.get("id")
        if chat_id is None or not text:
            return
        _handle_message(int(chat_id), text)
    except Exception:
        logger.exception("telegram update failed")


def _handle_message(chat_id: int, text: str) -> None:
    cmd = text.split()[0].split("@")[0].lower()
    if cmd in {"/start", "/help", "/connect"}:
        _cmd_start(chat_id, text, connect=cmd == "/connect")
        return
    user = _require_user(chat_id)
    if user is None:
        return
    if cmd in {"/status", "/agent"}:
        _cmd_agent(chat_id, user)
    elif cmd == "/balance":
        _cmd_balance(chat_id, user)
    elif cmd == "/positions":
        _cmd_positions(chat_id, user)
    elif cmd == "/activity":
        _cmd_activity(chat_id, user)
    elif cmd == "/pause":
        _cmd_pause_resume(chat_id, user, pause=True)
    elif cmd == "/resume":
        _cmd_pause_resume(chat_id, user, pause=False)
    elif cmd == "/traders":
        _cmd_traders(chat_id)
    elif cmd == "/follow":
        _cmd_follow(chat_id, user, text, auto=False)
    elif cmd == "/copy":
        _cmd_follow(chat_id, user, text, auto=True)
    elif cmd == "/following":
        _cmd_following(chat_id, user)
    elif cmd in {"/markets", "/events"}:
        _cmd_markets(chat_id)
    elif cmd == "/analyze":
        _cmd_analyze(chat_id, user, text)
    elif cmd == "/trade":
        _cmd_trade(chat_id, user, text)
    elif cmd.startswith("/"):
        _send(chat_id, "I didn’t catch that.\n\n" + HELP, reply_markup=_nav_keyboard())
    else:
        _handle_natural_language(chat_id, user, text)


def _require_user(chat_id: int):
    link = active_link_for_chat(chat_id)
    if not link:
        bot = bot_url()
        bot_bit = html_link(bot, "the DreamLens bot") if bot else "the DreamLens bot"
        _send(
            chat_id,
            "This chat is not linked yet.\n\n"
            f"1. Open {bot_bit} and tap Start\n"
            f"2. Copy this chat ID: <code>{chat_id}</code>\n"
            f"3. Paste it on {html_link(_portfolio_url(), 'Portfolio')}, then tap Confirm here.",
        )
        return None
    return link.user


def _linked_keyboard():
    return _nav_keyboard()


def _cmd_start(chat_id: int, text: str, *, connect: bool = False) -> None:
    parts = text.split(maxsplit=1)
    payload = parts[1] if len(parts) > 1 else ""
    if payload.startswith("ok_"):
        try:
            confirm_link(chat_id=chat_id, token=payload[3:])
            _send(
                chat_id,
                "<b>Telegram is linked</b> to your DreamLens wallet.\n\n" + HELP,
                reply_markup=_linked_keyboard(),
            )
        except TelegramLinkError as exc:
            _send(chat_id, _h(str(exc)))
        return
    if active_link_for_chat(chat_id):
        _send(
            chat_id,
            f"<b>Linked</b> to DreamLens. Chat ID <code>{chat_id}</code>\n\n" + HELP,
            reply_markup=_linked_keyboard(),
        )
        return
    _send(
        chat_id,
        f"Your Telegram chat ID is <code>{chat_id}</code>.\n"
        f"Paste it on {html_link(_portfolio_url(), 'Portfolio')}, then tap Confirm in this chat.\n\n"
        + HELP,
    )


def _user_agent(user):
    from apps.agents.models import DreamAgent

    return get_running_agent(user) or (
        DreamAgent.objects.filter(user=user)
        .exclude(status=DreamAgent.Status.REVOKED)
        .select_related("smart_account")
        .order_by("-updated_at")
        .first()
    )


def _cmd_agent(chat_id: int, user) -> None:
    from services import smart_account_service

    agent = _user_agent(user)
    if not agent:
        _send(
            chat_id,
            "No DreamAgent on this wallet. "
            f"{html_link(_activate_url(), 'Activate DreamAgent')} in the browser — "
            "Telegram cannot sign MetaMask.",
        )
        return
    perf = agent_performance(agent)
    sa = agent.smart_account
    bal = None
    try:
        bal = smart_account_service.get_balance(sa)
    except Exception:
        logger.warning("telegram agent balance failed", exc_info=True)
    coll = (bal or {}).get("collateral") or perf.get("balance") or "—"
    native = (bal or {}).get("native", "—")
    native_sym = (bal or {}).get("native_symbol", "STT")
    perm = perf.get("permission") or {}
    from apps.agents.models import DreamAgent

    copy_on = "On" if agent.status == DreamAgent.Status.RUNNING else "Off"
    status = _STATUS_LABEL.get(agent.status, agent.status)
    pnl = perf.get("pnl")
    pnl_line = format_collateral(pnl) if pnl not in (None, "—") else "—"
    lines = [
        f"<b>{_h(agent.name)}</b>",
        f"{_h(status)} · Smart Copy {copy_on}",
        "",
        f"Collateral  {_h(coll)}",
        f"Gas  {_h(native)} {_h(native_sym)}",
        f"Copied / skipped  {perf.get('trades_copied', 0)} / {perf.get('trades_skipped', 0)}",
        f"{_h('Today’s result')}  {_h(pnl_line)}",
        f"Max {_h(format_collateral(perm.get('max_trade_amount'), compact=True))} per trade · "
        f"{_h(format_collateral(perm.get('max_daily_volume'), compact=True))} / day",
    ]
    if agent.status == DreamAgent.Status.RUNNING:
        lines.append("\nDreamAgent claims Smart Account wins when a window settles.")
    _send(chat_id, "\n".join(lines), reply_markup=_agent_keyboard(agent))


def _cmd_balance(chat_id: int, user) -> None:
    from services import smart_account_service

    agent = _user_agent(user)
    if not agent:
        _send(
            chat_id,
            "No Smart Account yet. "
            f"{html_link(_activate_url(), 'Activate DreamAgent')} in the browser.",
        )
        return
    try:
        bal = smart_account_service.get_balance(agent.smart_account)
    except Exception:
        logger.warning("telegram balance failed", exc_info=True)
        _send(chat_id, "Could not read on-chain balances right now.")
        return
    addr = agent.smart_account.address
    _send(
        chat_id,
        "<b>Smart Account</b>\n"
        f"Collateral  {_h(bal.get('collateral', '—'))} {_h(bal.get('collateral_symbol', ''))}\n"
        f"Gas  {_h(bal.get('native', '—'))} {_h(bal.get('native_symbol', 'STT'))}\n"
        f"<code>{_h(addr)}</code>",
        reply_markup=_nav_keyboard(),
    )


def _cmd_positions(chat_id: int, user) -> None:
    from services.portfolio_service import annotate_positions, list_recent_trades, refresh_portfolio
    from apps.portfolio.models import Position

    refresh_portfolio(user)
    positions = annotate_positions(
        user,
        list(
            Position.objects.filter(user=user)
            .select_related("event", "outcome")
            .order_by("-opened_at")[:12]
        ),
    )
    trades = list_recent_trades(user, limit=8)
    if not positions and not trades:
        _send(
            chat_id,
            "No positions yet. Open a window on Discover or send /events.",
            reply_markup=_nav_keyboard(),
        )
        return
    lines = ["<b>Positions</b>"]
    for pos in positions:
        result = pos.result or "open"
        label = _RESULT_LABEL.get(result, result)
        amount = pos.amount
        amount_s = f"{amount:g}" if amount is not None else ""
        q = event_question(pos.event)
        extra = ""
        if getattr(pos, "claimable", False):
            extra = " · claim on Portfolio" if result in ("won", "void") else extra
        elif result == "won":
            extra = " · DreamAgent claims Smart Account wins"
        pnl = ""
        if pos.pnl is not None:
            pnl = f"\nToday’s result {_h(format_collateral(pos.pnl))}"
        lines.append(
            f"{_h(label)} {_h(pos.outcome.outcome_type)} {_h(amount_s)}"
            f"{_h(extra)}\n{_h(q)}{pnl}"
        )
    if trades:
        lines.append("\n<b>Trades</b>")
        for trade in trades:
            lines.append(
                f"{_h(trade.outcome.outcome_type)} {_h(as_cents(trade.entry_price))}"
                f" · {_h(event_question(trade.event))}"
            )
    _send(chat_id, "\n\n".join(lines), reply_markup=_nav_keyboard())


def _cmd_activity(chat_id: int, user) -> None:
    from apps.agents.models import AgentEvaluation

    agent = _user_agent(user)
    if not agent:
        _send(chat_id, "No DreamAgent evaluations yet.", reply_markup=_nav_keyboard())
        return
    rows = list(
        AgentEvaluation.objects.filter(agent=agent).order_by("-created_at")[:10]
    )
    if not rows:
        _send(chat_id, "No agent activity yet.", reply_markup=_nav_keyboard())
        return
    lines = ["<b>Agent activity</b>"]
    for ev in rows:
        title = ev.event_title or "event"
        amt = f" {_h(format_collateral(ev.amount, compact=True))}" if ev.amount is not None else ""
        decision = _DECISION_LABEL.get(ev.decision, ev.decision)
        line = f"{_h(decision)}{amt} · {_h(title)}"
        anchor = explorer_tx_anchor(ev.tx_hash or "")
        if anchor:
            line += f"\n{anchor}"
        lines.append(line)
    _send(chat_id, "\n\n".join(lines), reply_markup=_nav_keyboard())


def _cmd_pause_resume(chat_id: int, user, *, pause: bool) -> None:
    from apps.agents.models import DreamAgent
    from services.smart_account_service import SmartAccountError, set_agent_status

    target = DreamAgent.Status.PAUSED if pause else DreamAgent.Status.RUNNING
    try:
        agent = set_agent_status(user, target)
    except SmartAccountError as exc:
        _send(chat_id, _h(str(exc)))
        return
    if pause:
        _send(
            chat_id,
            f"<b>{_h(agent.name)}</b> is paused.\n"
            "Smart Copy is off. Send /resume, then /trade.",
            reply_markup=_agent_keyboard(agent),
        )
        return
    _send(
        chat_id,
        f"<b>{_h(agent.name)}</b> is running.\n"
        "Smart Copy is on. DreamAgent will claim Smart Account wins when windows settle.",
        reply_markup=_agent_keyboard(agent),
    )


def _cmd_traders(chat_id: int) -> None:
    traders = list_active_traders(limit=12)
    if not traders:
        _send(chat_id, "No on-chain traders indexed yet. Try again in a minute.")
        return
    lines = ["<b>On-chain traders</b>"]
    rows = []
    for t in traders[:8]:
        wr = t.win_rate or Decimal("0")
        wr_pct = wr if wr > 1 else wr * 100
        name = t.display_name or (t.wallet_address[:8] + "…")
        vol = format_collateral(t.total_volume, compact=True)
        lines.append(
            f"<b>#{t.pk} {_h(name)}</b>\n"
            f"Score {int(t.trader_score or 0)} · {wr_pct:.0f}% observed · vol {_h(vol)}"
        )
        short = (name[:14] + "…") if len(name) > 15 else name
        rows.append(
            [
                (f"Follow {short}", f"fl:{t.pk}"),
                (f"Copy {short}", f"cp:{t.pk}"),
            ]
        )
    _send(chat_id, "\n\n".join(lines), reply_markup=inline_keyboard(rows))


def _cmd_follow(chat_id: int, user, text: str, *, auto: bool) -> None:
    parts = text.split()
    if len(parts) < 2:
        _send(
            chat_id,
            "Usage: /follow 12  or  /copy 12\n"
            "You can also paste a 0x wallet. /traders lists IDs.",
        )
        return
    arg = parts[1]
    if arg.lower().startswith("0x"):
        _do_follow(chat_id, user, auto=auto, wallet_address=arg)
        return
    if not arg.isdigit():
        _send(
            chat_id,
            "Usage: /follow 12  or  /copy 12\n"
            "You can also paste a 0x wallet. /traders lists IDs.",
        )
        return
    _do_follow(chat_id, user, int(arg), auto=auto)


def _do_follow(
    chat_id: int,
    user,
    trader_id: int | None = None,
    *,
    auto: bool,
    wallet_address: str | None = None,
) -> None:
    from apps.dreamcopy.models import TraderProfile

    payload = {
        "copy_mode": CopyRelationship.CopyMode.SMART,
        "auto_execute": False,
        "max_per_trade": Decimal("10"),
        "max_daily": Decimal("50"),
        "min_copy_score": 70,
    }
    if wallet_address:
        payload["wallet_address"] = wallet_address
    elif trader_id:
        if not TraderProfile.objects.filter(pk=trader_id).exists():
            _send(chat_id, f"Trader {trader_id} not found.")
            return
        payload["trader_id"] = trader_id
    else:
        _send(chat_id, "Trader not found.")
        return
    running = get_running_agent(user) is not None
    payload["auto_execute"] = bool(auto and running)
    try:
        rel = create_copy_relationship(user, payload)
    except (CopyError, Exception) as exc:
        _send(chat_id, _h(str(exc)))
        return
    name = rel.trader.display_name or rel.trader.wallet_address
    label = f"#{rel.trader_id}"
    if auto and not running:
        _send(
            chat_id,
            f"Following <b>{_h(name)}</b> ({_h(label)}).\n"
            "Auto-copy needs an Active DreamAgent — "
            f"{html_link(_activate_url(), 'Activate DreamAgent')}.",
        )
        return
    if payload["auto_execute"]:
        _send(
            chat_id,
            f"<b>Smart Copy is on</b> for {_h(name)} ({_h(label)}).\n"
            "DreamAgent will copy when the score clears your limit.",
        )
        return
    _send(
        chat_id,
        f"<b>Following</b> {_h(name)} ({_h(label)}).\n"
        "You’ll get a Telegram alert when they trade. DreamAgent will not copy until you /copy or turn Smart Copy on.",
    )


def _cmd_following(chat_id: int, user) -> None:
    rows = (
        CopyRelationship.objects.filter(user=user, status=CopyRelationship.Status.ACTIVE)
        .select_related("trader")
        .order_by("-updated_at")[:15]
    )
    if not rows:
        _send(
            chat_id,
            "You are not following anyone yet. Send /traders to pick someone.",
            reply_markup=_nav_keyboard(),
        )
        return
    lines = ["<b>Following</b>"]
    for rel in rows:
        name = rel.trader.display_name or rel.trader.wallet_address
        auto = "Smart Copy" if rel.auto_execute else "Alert only"
        lines.append(f"#{rel.trader_id} {_h(name)} · {_h(auto)}")
    _send(chat_id, "\n".join(lines), reply_markup=_nav_keyboard())


def _live_events(*, asset: str | None = None):
    qs = (
        EventContract.objects.filter(
            status__in=[EventContract.Status.TRADING, EventContract.Status.LIVE]
        )
        .filter(expiry_time__gt=timezone.now())
        .prefetch_related("outcomes")
        .order_by("expiry_time")
    )
    if asset:
        qs = qs.filter(underlying_asset__iexact=asset)
    return list(qs[:8])


def _cmd_markets(chat_id: int, *, asset: str | None = None) -> None:
    events = _live_events(asset=asset)
    if not events:
        _send(chat_id, "No live Event Contracts right now. Check back when a window opens.")
        return
    lines = ["<b>Live Event Contracts</b>"]
    rows = []
    for ev in events:
        lines.append(f"<b>#{ev.pk}</b>\n{_h(format_event_card_text(ev))}")
        rows.append(
            [
                ("Explain", f"an:{ev.pk}"),
                ("YES", f"by:{ev.pk}:YES"),
                ("NO", f"by:{ev.pk}:NO"),
            ]
        )
    lines.append("Or /trade 12 YES 5")
    _send(chat_id, "\n\n".join(lines), reply_markup=inline_keyboard(rows))


def _resolve_event(*, event_id: int | None = None, asset: str | None = None) -> EventContract | None:
    if event_id:
        return EventContract.objects.prefetch_related("outcomes").filter(pk=event_id).first()
    events = _live_events(asset=asset)
    if not events:
        return None
    if asset:
        return events[0]
    if len(events) == 1:
        return events[0]
    return None


def _cmd_analyze(chat_id: int, user, text: str) -> None:
    match = _ANALYZE_RE.match(text.strip())
    token = (match.group(1) if match else "") or ""
    event = None
    if token.isdigit():
        event = _resolve_event(event_id=int(token))
    elif token:
        asset = token.upper()
        if asset in {"BITCOIN"}:
            asset = "BTC"
        if asset in {"ETHEREUM"}:
            asset = "ETH"
        event = _resolve_event(asset=asset)
        if event is None:
            _cmd_markets(chat_id, asset=asset if asset in {"BTC", "ETH"} else None)
            return
    else:
        event = _resolve_event()
        if event is None:
            _cmd_markets(chat_id)
            return
    if event is None:
        _send(chat_id, "Event not found. Send /events.")
        return
    _send_analysis(chat_id, user, event)


def _send_analysis(chat_id: int, user, event: EventContract) -> None:
    try:
        insight = analyze_event(event, user=user)
    except Exception:
        logger.warning("telegram analyze failed", exc_info=True)
        insight = {}
    brief = watching_brief(event)
    reasons = insight.get("reasons") or []
    reason = reasons[0] if reasons else brief.get("lead") or "Market data from DreamDEX."
    yes, no = yes_no_outcomes(event)
    lines = [
        f"<b>{_h(event_question(event))}</b>",
        f"YES {_h(as_cents(yes.current_price if yes else None))} · "
        f"NO {_h(as_cents(no.current_price if no else None))}",
        _h(format_window_line(event)),
        _h(reason),
        _h(brief.get("reading") or ""),
        _h(SCORE_DISCLAIMER),
        "This is a market price, not a guarantee.",
    ]
    _send(
        chat_id,
        "\n".join(str(x) for x in lines if x),
        reply_markup=inline_keyboard(
            [
                [
                    ("YES", f"by:{event.pk}:YES"),
                    ("NO", f"by:{event.pk}:NO"),
                ]
            ]
        ),
    )


def _max_trade(user) -> Decimal | None:
    agent = get_tradable_agent(user)
    if not agent:
        return None
    perm = active_permission(agent)
    if not perm:
        return None
    return perm.max_trade_amount


def _default_trade_amount(user) -> Decimal:
    cap = _max_trade(user) or Decimal("5")
    return min(Decimal("5"), cap)


def _put_pending_trade(user, token: str, payload: dict) -> None:
    """Cache + DB so Confirm still works when Redis IGNORE_EXCEPTIONS drops the key."""
    cache.set(f"tg:trade:{token}", payload, TRADE_TTL)
    agent = get_tradable_agent(user)
    if not agent:
        return
    now = timezone.now().timestamp()
    meta = dict(agent.metadata_json or {})
    pending = {
        key: row
        for key, row in dict(meta.get(_TG_TRADES_KEY) or {}).items()
        if float((row or {}).get("exp") or 0) > now
    }
    pending[token] = {**payload, "exp": now + TRADE_TTL}
    meta[_TG_TRADES_KEY] = pending
    agent.metadata_json = meta
    agent.save(update_fields=["metadata_json", "updated_at"])


def _pop_pending_trade(user, token: str) -> dict | None:
    key = f"tg:trade:{token}"
    payload = cache.get(key)
    cache.delete(key)
    agent = get_tradable_agent(user) or _user_agent(user)
    if agent:
        meta = dict(agent.metadata_json or {})
        pending = dict(meta.get(_TG_TRADES_KEY) or {})
        row = pending.pop(token, None)
        if meta.get(_TG_TRADES_KEY) != pending:
            meta[_TG_TRADES_KEY] = pending
            agent.metadata_json = meta
            agent.save(update_fields=["metadata_json", "updated_at"])
        if payload is None and isinstance(row, dict):
            exp = float(row.get("exp") or 0)
            if exp and exp < timezone.now().timestamp():
                return None
            payload = {k: v for k, v in row.items() if k != "exp"}
    return payload


def _require_running_agent(chat_id: int, user) -> bool:
    from apps.agents.models import DreamAgent

    if get_tradable_agent(user):
        return True
    agent = _user_agent(user)
    if agent and agent.status == DreamAgent.Status.PAUSED:
        _send(chat_id, "DreamAgent is paused. Send /resume to trade.")
        return False
    if agent and agent.status == DreamAgent.Status.EXPIRED:
        _send(
            chat_id,
            f"Your DreamAgent grant expired. Re-sign it at {html_link(_activate_url(), 'Activate DreamAgent')} "
            "in the browser — Telegram cannot use MetaMask.",
        )
        return False
    _send(
        chat_id,
        f"{html_link(_activate_url(), 'Activate DreamAgent')} in the browser — "
        "Telegram cannot sign MetaMask.",
    )
    return False


def _send_trade_result(chat_id: int, user, *, event_id: int, outcome: str, amount: Decimal) -> None:
    try:
        trade = execute_agent_manual_trade(
            user,
            event_id=int(event_id),
            outcome=str(outcome),
            amount=Decimal(str(amount)),
        )
    except DreamAgentError as exc:
        msg = str(exc)
        cap = _max_trade(user)
        if cap is not None and "exceeds max" in msg.lower():
            _send(
                chat_id,
                LIMIT_TEMPLATE.format(
                    max=format_collateral(cap, compact=True),
                    asked=format_collateral(amount, compact=True),
                ),
            )
        else:
            _send(chat_id, _h(msg))
        return
    tx = trade.transaction_hash or ""
    event = getattr(trade, "event", None)
    q = event_question(event) if event is not None else "Event Contract"
    side = getattr(getattr(trade, "outcome", None), "outcome_type", "") or ""
    stake = format_collateral(trade.amount, compact=True) if getattr(trade, "amount", None) is not None else ""
    anchor = explorer_tx_anchor(tx)
    lines = [
        "<b>Trade submitted</b>",
        f"{_h(side)} {_h(stake)} · {_h(q)}".strip(" ·"),
    ]
    if anchor:
        lines.append(anchor)
    lines.append("DreamAgent will claim Smart Account wins when this window settles.")
    _send(chat_id, "\n".join(lines), reply_markup=_nav_keyboard())


def _offer_trade(chat_id: int, user, event: EventContract, outcome: str, amount: Decimal) -> None:
    """Session-key redeem — no MetaMask popup and no extra Telegram confirm."""
    if not _require_running_agent(chat_id, user):
        return
    cap = _max_trade(user)
    if cap is not None and amount > cap:
        _send(
            chat_id,
            LIMIT_TEMPLATE.format(
                max=format_collateral(cap, compact=True),
                asked=format_collateral(amount, compact=True),
            ),
        )
        return
    yes, no = yes_no_outcomes(event)
    side = yes if outcome.upper() == "YES" else no
    price = side.current_price if side else None
    payout = format_payout_block(amount, price)
    _send(
        chat_id,
        "\n".join(
            [
                f"<b>{_h(event_question(event))}</b>",
                f"{_h(outcome)} {_h(as_cents(price))}",
                _h(payout),
                _h(format_window_line(event)),
                "DreamAgent is placing this on-chain.",
            ]
        ),
    )
    _send_trade_result(chat_id, user, event_id=event.pk, outcome=outcome, amount=amount)


def _cmd_trade(chat_id: int, user, text: str) -> None:
    match = _TRADE_RE.match(text.strip())
    if not match:
        _send(
            chat_id,
            "Usage: /trade 12 YES 5\n"
            "That’s event id, side, and dollars. Example: /trade 12 YES 10",
        )
        return
    if not _require_running_agent(chat_id, user):
        return
    event_id = int(match.group(1))
    outcome = match.group(2).upper()
    try:
        amount = Decimal(match.group(3))
    except InvalidOperation:
        _send(chat_id, "Amount must be a number.")
        return
    event = EventContract.objects.filter(pk=event_id).first()
    if not event:
        _send(chat_id, f"Event {event_id} not found. Send /events.")
        return
    _offer_trade(chat_id, user, event, outcome, amount)


def _handle_natural_language(chat_id: int, user, text: str) -> None:
    lower = text.strip().lower()
    if any(p in lower for p in _PAUSE_PHRASES):
        _cmd_pause_resume(chat_id, user, pause=True)
        return
    if any(p in lower for p in _RESUME_PHRASES):
        _cmd_pause_resume(chat_id, user, pause=False)
        return
    if any(p in lower for p in _POSITION_PHRASES):
        _cmd_positions(chat_id, user)
        return
    if any(p in lower for p in _AGENT_PHRASES):
        _cmd_agent(chat_id, user)
        return

    parsed = parse_intent(text)
    if parsed.intent == "PREPARE_TRADE":
        amount = Decimal(str(parsed.params["amount"]))
        outcome = str(parsed.params["outcome"])
        asset = parsed.params.get("asset")
        event = _resolve_event(asset=asset)
        if event is None:
            _send(
                chat_id,
                "Which event? Send /events, then /trade "
                f"{_h(outcome)} {_h(amount)}.",
            )
            _cmd_markets(chat_id, asset=asset)
            return
        _offer_trade(chat_id, user, event, outcome, amount)
        return
    if parsed.intent == "SEARCH_EVENTS":
        _cmd_markets(chat_id, asset=parsed.params.get("asset") or None)
        return
    if parsed.intent == "GET_PORTFOLIO":
        _cmd_positions(chat_id, user)
        return
    if parsed.intent == "ANALYZE_EVENT":
        event = _resolve_event()
        if event is None:
            _cmd_markets(chat_id)
            return
        _send_analysis(chat_id, user, event)
        return
    _send(chat_id, "I didn’t catch that.\n\n" + HELP, reply_markup=_nav_keyboard())


def _handle_callback(query: dict[str, Any]) -> None:
    data = query.get("data") or ""
    callback_id = query.get("id") or ""
    chat = (query.get("message") or {}).get("chat") or query.get("from") or {}
    chat_id = chat.get("id")
    if chat_id is None:
        return
    chat_id = int(chat_id)

    if data.startswith("tg:ok:"):
        token = data[6:]
        try:
            confirm_link(chat_id=chat_id, token=token)
            answer_callback(callback_id, "Linked")
            _send(
                chat_id,
                "<b>Telegram is linked</b> to your DreamLens wallet.\n\n" + HELP,
                reply_markup=_linked_keyboard(),
            )
        except TelegramLinkError as exc:
            answer_callback(callback_id, "Could not link")
            _send(chat_id, _h(str(exc)))
        return
    if data.startswith("tg:no:"):
        token = data[6:]
        cancel_pending_link(chat_id=chat_id, token=token)
        answer_callback(callback_id, "Cancelled")
        _send(
            chat_id,
            "Link cancelled. Paste a chat ID on Portfolio when you are ready.",
        )
        return

    user = _require_user(chat_id)
    if user is None:
        answer_callback(callback_id)
        return

    if data.startswith("go:"):
        answer_callback(callback_id)
        dest = data[3:]
        if dest == "events":
            _cmd_markets(chat_id)
        elif dest == "agent":
            _cmd_agent(chat_id, user)
        elif dest == "pos":
            _cmd_positions(chat_id, user)
        elif dest == "traders":
            _cmd_traders(chat_id)
        elif dest == "pause":
            _cmd_pause_resume(chat_id, user, pause=True)
        elif dest == "resume":
            _cmd_pause_resume(chat_id, user, pause=False)
        elif dest == "activity":
            _cmd_activity(chat_id, user)
        elif dest == "help":
            _send(chat_id, HELP, reply_markup=_nav_keyboard())
        return

    if data.startswith("an:"):
        try:
            event_id = int(data[3:])
        except ValueError:
            answer_callback(callback_id, "Bad event")
            return
        answer_callback(callback_id)
        event = _resolve_event(event_id=event_id)
        if not event:
            _send(chat_id, "Event not found.")
            return
        _send_analysis(chat_id, user, event)
        return

    if data.startswith("by:"):
        parts = data.split(":")
        if len(parts) != 3:
            answer_callback(callback_id, "Bad trade")
            return
        try:
            event_id = int(parts[1])
        except ValueError:
            answer_callback(callback_id, "Bad event")
            return
        outcome = parts[2].upper()
        answer_callback(callback_id)
        event = _resolve_event(event_id=event_id)
        if not event:
            _send(chat_id, "Event not found.")
            return
        _offer_trade(chat_id, user, event, outcome, _default_trade_amount(user))
        return

    if data.startswith("fl:") or data.startswith("cp:"):
        auto = data.startswith("cp:")
        try:
            trader_id = int(data[3:])
        except ValueError:
            answer_callback(callback_id, "Bad trader")
            return
        answer_callback(callback_id)
        _do_follow(chat_id, user, trader_id, auto=auto)
        return

    if data.startswith("tr:ok:"):
        token = data[6:]
        payload = _pop_pending_trade(user, token)
        if not payload or int(payload.get("user_id") or 0) != user.pk:
            answer_callback(callback_id, "Expired")
            _send(chat_id, "That trade request expired. Send /trade again.")
            return
        answer_callback(callback_id, "Sending")
        _send_trade_result(
            chat_id,
            user,
            event_id=int(payload["event_id"]),
            outcome=str(payload["outcome"]),
            amount=Decimal(str(payload["amount"])),
        )
        return
    if data.startswith("tr:no:"):
        token = data[6:]
        _pop_pending_trade(user, token)
        answer_callback(callback_id, "Cancelled")
        _send(chat_id, "Trade cancelled.")
        return

    answer_callback(callback_id)
