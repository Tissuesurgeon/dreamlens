"""DreamLens page views — landing, home, discover, lens, event detail, portfolio, following, traders."""

from __future__ import annotations

import json
import logging
from decimal import Decimal
from types import SimpleNamespace

from django.db.models import Count, Q
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from apps.dreamcopy.models import CopyExecution, CopyRelationship, TraderProfile
from apps.events.models import EventContract, EventOutcome, EventRadarSignal
from apps.portfolio.models import Position
from integrations.dreamdex.trading import get_candles
from services.ai_service import analyze_event
from services.event_copy import (
    SCORE_DISCLAIMER,
    annotate_event_display,
    asset_mix_groups,
    format_collateral,
    trader_view_signals,
)
from services.event_service import refresh_event_from_dreamdex, refresh_events_from_dreamdex
from services.market_stats import event_market_stats
from services.trader_service import (
    build_trader_analytics,
    list_active_traders,
)

logger = logging.getLogger("dreamlens.views")

RADAR_TILES = [
    {
        "signal_type": "STRONG_CONSENSUS",
        "title": "Consensus",
        "blurb": "Top traders are heavily aligned on these events.",
    },
    {
        "signal_type": "MOVING_FAST",
        "title": "Moving fast",
        "blurb": "Prices are shifting quickly — watch momentum.",
    },
    {
        "signal_type": "TRADER_DIVERGENCE",
        "title": "Divergence",
        "blurb": "Experienced traders are disagreeing.",
    },
    {
        "signal_type": "EXPIRING_SOON",
        "title": "Expiring soon",
        "blurb": "Windows closing — last chance to trade.",
    },
]

LENS_CHIPS = [
    "What's moving right now?",
    "News hitting BTC?",
    "Which events expire soon?",
]

INTENT_FILTERS = [
    {"id": "all", "label": "All"},
    {"id": "moving-fast", "label": "Moving Fast"},
    {"id": "popular", "label": "Popular"},
    {"id": "high-score", "label": "High DreamLens Score"},
    {"id": "traders-active", "label": "Traders Are Active"},
    {"id": "ending-soon", "label": "Ending Soon"},
]


def _yes_no_outcomes(event: EventContract) -> tuple[EventOutcome | None, EventOutcome | None]:
    yes = no = None
    for outcome in event.outcomes.all():
        if outcome.outcome_type == EventOutcome.OutcomeType.YES:
            yes = outcome
        elif outcome.outcome_type == EventOutcome.OutcomeType.NO:
            no = outcome
    return yes, no


# UI thresholds — wider than the worker (15m) so tiles match the live list.
_UI_CONSENSUS = Decimal("0.65")
_UI_DIVERGENCE_LOW = Decimal("0.42")
_UI_DIVERGENCE_HIGH = Decimal("0.58")
_UI_MOVE_DELTA = Decimal("0.10")
_UI_EXPIRING_MINUTES = 180
_UI_MOVING_MINUTES = 60
_CARD_SIGNAL_PRIORITY = (
    "MOVING_FAST",
    "EXPIRING_SOON",
    "STRONG_CONSENSUS",
    "TRADER_DIVERGENCE",
)


def _minutes_left(event: EventContract, now) -> float:
    if not event.expiry_time:
        return 0
    return (event.expiry_time - now).total_seconds() / 60


def _radar_types_for_event(event: EventContract, now) -> list[str]:
    """Classify a live event for Event Radar without stale DB rows."""
    types: list[str] = []
    yes, _ = _yes_no_outcomes(event)
    price = yes.current_price if yes else None
    mins = _minutes_left(event, now)

    if price is not None:
        if price >= _UI_CONSENSUS or price <= (Decimal("1") - _UI_CONSENSUS):
            types.append("STRONG_CONSENSUS")
        if _UI_DIVERGENCE_LOW <= price <= _UI_DIVERGENCE_HIGH:
            types.append("TRADER_DIVERGENCE")
        if 0 < mins <= _UI_MOVING_MINUTES and abs(price - Decimal("0.5")) >= _UI_MOVE_DELTA:
            types.append("MOVING_FAST")

    if 0 < mins <= _UI_EXPIRING_MINUTES:
        types.append("EXPIRING_SOON")

    for signal in event.radar_signals.all():
        if (
            signal.is_active
            and signal.signal_type in {t["signal_type"] for t in RADAR_TILES}
            and signal.signal_type not in types
        ):
            types.append(signal.signal_type)
    return types


def _build_radar(markets: list[EventContract]) -> list[dict]:
    """Build radar tiles and stamp each live card with matching signal types."""
    now = timezone.now()
    buckets: dict[str, list[int]] = {tile["signal_type"]: [] for tile in RADAR_TILES}

    for event in markets:
        types = _radar_types_for_event(event, now)
        event.radar_types = types  # noqa: SLF001 — template helper
        best = next((t for t in _CARD_SIGNAL_PRIORITY if t in types), None)
        event.card_signal = (
            SimpleNamespace(signal_type=best, score=None) if best else None
        )
        for signal_type in types:
            if signal_type in buckets and event.pk not in buckets[signal_type]:
                buckets[signal_type].append(event.pk)

    return [
        {
            **tile,
            "event_ids": buckets[tile["signal_type"]],
            "signals": buckets[tile["signal_type"]],
        }
        for tile in RADAR_TILES
    ]


def _ai_insight(event: EventContract, yes: EventOutcome | None) -> dict:
    """Build AI Lens context from structured analyze_event output."""
    try:
        analysis = analyze_event(event)
    except Exception:
        logger.exception("analyze_event failed for %s", event.pk)
        analysis = {}

    yes_price = yes.current_price if yes else Decimal("0.5")
    market_prob = float(analysis.get("market_probability") or yes_price)
    estimate = float(analysis.get("estimated_probability") or (float(yes_price) + 0.06))
    estimate = min(max(estimate, 0.05), 0.95)
    market_pct = int(round(market_prob * 100))
    estimate_pct = int(round(estimate * 100))
    market_price = f"{market_prob:.2f}"
    edge = estimate_pct - market_pct
    direction = "YES" if edge >= 0 else "NO"
    reasons = analysis.get("reasons") or [
        f"YES is the live book for whether {event.underlying_asset} finishes above the strike.",
        f"{event.trade_count} trades and {event.cumulative_quote_volume} volume show how active this window is.",
        f"{event.minutes_to_expiry:.1f} minutes remain — a last print can still change the result.",
        "DreamLens Score is an activity signal, not a chance of winning.",
        "A headline in the underlying can reprice YES faster than this window can absorb.",
    ]
    risks = analysis.get("risks") or [
        "Short expiry — price can reverse quickly.",
        "Liquidity may thin near settlement.",
        "Oracle timing can differ from spot moves.",
        "A headline can reprice the underlying before this window closes.",
    ]
    return {
        "estimate": Decimal(str(round(estimate, 2))),
        "estimate_pct": estimate_pct,
        "market_pct": market_pct,
        "market_price": market_price,
        "edge": abs(edge),
        "direction": direction,
        "setup": analysis.get("setup") or "",
        "summary": (
            f"DreamLens estimate: {estimate_pct}% YES vs market {format_collateral(market_price)}. "
            "Analytical estimate — not a guaranteed probability."
        ),
        "why": reasons[:5],
        "risks": risks[:4],
        "confidence": float(analysis.get("confidence") or 0.65),
        "signal": analysis.get("signal") or "INTERESTING",
    }


def _chart_data(event: EventContract) -> dict:
    labels: list[str] = []
    prices: list[float] = []
    try:
        if event.pool_address:
            candles = get_candles(event.pool_address, interval=60)
            for c in candles:
                labels.append(c.timestamp.strftime("%H:%M"))
                prices.append(float(c.close))
    except Exception:
        logger.debug("chart data unavailable for event %s", event.pk)

    if not prices:
        yes, _ = _yes_no_outcomes(event)
        base = float(yes.current_price) if yes else 0.5
        prices = [base - 0.03, base - 0.01, base, base + 0.02, base]
        labels = ["T-4", "T-3", "T-2", "T-1", "Now"]

    return {"labels": labels, "prices": prices}


def start(request):
    """Info page: how to set up the Dream Agent. The controls live on /agent/activate/."""
    from services.onboarding_service import (
        STT_FAUCET_URL,
        TEST_USDC_FAUCET_URL,
        setup_guide,
    )

    return render(
        request,
        "onboarding/start.html",
        {
            "guide": setup_guide(),
            "stt_faucet": STT_FAUCET_URL,
            "usdc_faucet": TEST_USDC_FAUCET_URL,
        },
    )


def landing(request):
    """Marketing landing — no DreamDEX sync; CTA into the app."""
    return render(request, "landing.html", {})


def _live_markets() -> tuple[list[EventContract], list[dict]]:
    refresh_events_from_dreamdex()
    now = timezone.now()
    markets = list(
        EventContract.objects.filter(
            status__in=[EventContract.Status.TRADING, EventContract.Status.LIVE],
            expiry_time__gt=now,
        )
        .prefetch_related("outcomes", "radar_signals")
        .order_by("expiry_time")[:24]
    )
    radar = _build_radar(markets)
    for event in markets:
        annotate_event_display(event)
    return markets, radar


def _featured_and_watching(markets: list[EventContract]) -> tuple[EventContract | None, list[EventContract]]:
    if not markets:
        return None, []
    moving = [e for e in markets if "moving-fast" in (getattr(e, "intent_tags", []) or [])]
    ranked = sorted(
        markets,
        key=lambda e: (
            1 if "moving-fast" in (getattr(e, "intent_tags", []) or []) else 0,
            (getattr(e, "dl_score", {}) or {}).get("score", 0),
            e.trade_count or 0,
        ),
        reverse=True,
    )
    featured = moving[0] if moving else ranked[0]
    watching = [e for e in ranked if e.pk != featured.pk][:3]
    return featured, watching


def home(request):
    markets, _radar = _live_markets()
    featured, watching = _featured_and_watching(markets)

    agent = None
    agent_status = "off"
    available = None
    executions = []
    copies = []
    if request.user.is_authenticated:
        from apps.agents.models import DreamAgent
        from services import smart_account_service

        agent = (
            DreamAgent.objects.filter(user=request.user)
            .exclude(status=DreamAgent.Status.REVOKED)
            .select_related("smart_account")
            .order_by("-updated_at")
            .first()
        )
        if agent:
            if agent.status == DreamAgent.Status.RUNNING:
                agent_status = "active"
            elif agent.status == DreamAgent.Status.PAUSED:
                agent_status = "paused"
            else:
                agent_status = "ready"
        sa = smart_account_service.get_account(request.user)
        if sa:
            try:
                bal = smart_account_service.get_balance(sa)
                available = (bal or {}).get("collateral") if isinstance(bal, dict) else getattr(bal, "collateral", None)
            except Exception:
                available = None
        copies = list(
            CopyRelationship.objects.filter(
                user=request.user,
                status=CopyRelationship.Status.ACTIVE,
            ).select_related("trader")[:4]
        )
        executions = list(
            CopyExecution.objects.filter(relationship__user=request.user)
            .select_related(
                "relationship__trader",
                "source_trade__event",
                "source_trade__outcome",
                "copied_trade",
            )
            .order_by("-created_at")[:6]
        )
        for ex in executions:
            annotate_event_display(ex.source_trade.event)

    from services.onboarding_service import first_session_state

    return render(
        request,
        "home_app.html",
        {
            "featured": featured,
            "watching": watching,
            "live_count": len(markets),
            "agent": agent,
            "agent_status": agent_status,
            "available": available,
            "copy_relationships": copies,
            "executions": executions,
            "score_disclaimer": SCORE_DISCLAIMER,
            "first_session": first_session_state(request.user),
        },
    )


def discover(request):
    from services.onboarding_service import first_session_state

    markets, radar = _live_markets()
    state = first_session_state(request.user)
    return render(
        request,
        "home.html",
        {
            "markets": markets,
            "radar_tiles": radar,
            "intent_filters": INTENT_FILTERS,
            "score_disclaimer": SCORE_DISCLAIMER,
            "first_session": state,
        },
    )


def explore(request):
    return redirect("discover", permanent=False)


def lens(request):
    return render(
        request,
        "lens/index.html",
        {"lens_chips": LENS_CHIPS},
    )


def event_detail(request, pk: int):
    event = get_object_or_404(
        EventContract.objects.prefetch_related("outcomes"),
        pk=pk,
    )
    event = refresh_event_from_dreamdex(event)
    event = (
        EventContract.objects.prefetch_related("outcomes")
        .filter(pk=event.pk)
        .first()
        or event
    )
    yes, no = _yes_no_outcomes(event)
    annotate_event_display(event, trader_count=None)
    ai_insight = _ai_insight(event, yes)
    chart_data = _chart_data(event)

    stats = event_market_stats(event)
    traders = stats["traders"]
    liquidity = stats["liquidity"]
    volume = stats["volume"]
    trade_count = stats["trade_count"]
    trader_count = stats["trader_count"]
    yes_position_share = stats["yes_position_share"]
    no_position_share = stats["no_position_share"]
    annotate_event_display(event, trader_count=trader_count)

    yes_payout = None
    no_payout = None
    if yes and yes.current_price and yes.current_price > 0:
        yes_payout = (Decimal("1") / yes.current_price).quantize(Decimal("0.01"))
    if no and no.current_price and no.current_price > 0:
        no_payout = (Decimal("1") / no.current_price).quantize(Decimal("0.01"))

    consensus = None
    try:
        from services.consensus_service import compute_consensus

        consensus = compute_consensus(event)
    except Exception:
        logger.debug("consensus unavailable for event %s", event.pk)

    active_copy_trader_ids: set[int] = set()
    user_positions: list = []
    if request.user.is_authenticated:
        active_copy_trader_ids = set(
            CopyRelationship.objects.filter(
                user=request.user,
                status=CopyRelationship.Status.ACTIVE,
            ).values_list("trader_id", flat=True)
        )
        from services.portfolio_service import annotate_positions

        user_positions = annotate_positions(
            request.user,
            list(
                Position.objects.filter(
                    user=request.user,
                    event=event,
                ).select_related("event", "outcome")
            ),
        )

    return render(
        request,
        "events/detail.html",
        {
            "event": event,
            "yes_outcome": yes,
            "no_outcome": no,
            "yes_payout": yes_payout,
            "no_payout": no_payout,
            "ai_insight": ai_insight,
            "traders": traders,
            "chart_data": chart_data,
            "volume": volume,
            "liquidity": liquidity,
            "trade_count": trade_count,
            "trader_count": trader_count,
            "yes_position_share": yes_position_share,
            "no_position_share": no_position_share,
            "consensus": consensus,
            "active_copy_trader_ids": active_copy_trader_ids,
            "user_positions": user_positions,
            "score_disclaimer": SCORE_DISCLAIMER,
            "event_question": event.question,
        },
    )


def portfolio(request):
    if not request.user.is_authenticated:
        return render(
            request,
            "portfolio/index.html",
            {
                "total_pnl": Decimal("0"),
                "today_result": Decimal("0"),
                "available": None,
                "in_active_events": Decimal("0"),
                "potential_payout": Decimal("0"),
                "open_positions": [],
                "settled_positions": [],
                "closed_positions": [],
                "settling_positions": [],
                "agent_claimable": [],
                "has_positions": False,
                "event_count": 0,
                "won_count": 0,
                "lost_count": 0,
                "wallet_balances": None,
                "is_authenticated": False,
                "agent": None,
                "smart_account": None,
                "agent_performance": None,
                "agent_balance": None,
                "telegram_link": None,
                "recent_trades": [],
                "closed_lookback": "7d",
                "closed_lookbacks": (),
                "closed_total": 0,
                "recent_trades_total": 0,
                "copy_relationships": [],
                "pending_copy_count": 0,
                "agent_can_auto_copy": False,
                "grant_health": {"needs_resign": False, "reasons": []},
                "book": None,
            },
        )

    from apps.agents.models import DreamAgent
    from services import dream_agent_service, smart_account_service
    from services.portfolio_service import (
        CLOSED_LOOKBACKS,
        annotate_positions,
        closed_lookback_cutoff,
        get_portfolio_summary,
        get_wallet_balances_for_user,
        in_closed_lookback,
        list_recent_trades,
        parse_closed_lookback,
        refresh_portfolio,
    )
    from services.telegram_link_service import get_link, serialize_link

    refresh_portfolio(request.user)
    wallet_balances = get_wallet_balances_for_user(request.user)
    smart_account = smart_account_service.get_account(request.user)
    agent = (
        DreamAgent.objects.filter(user=request.user)
        .exclude(status=DreamAgent.Status.REVOKED)
        .select_related("smart_account")
        .order_by("-updated_at")
        .first()
    )
    agent_balance = None
    if smart_account:
        try:
            agent_balance = smart_account_service.get_balance(smart_account)
        except Exception:
            logger.warning("portfolio agent balance unavailable", exc_info=True)
            agent_balance = None
    agent_performance = (
        dream_agent_service.agent_performance(agent, balance=agent_balance)
        if agent
        else None
    )

    positions = annotate_positions(
        request.user,
        list(
            Position.objects.filter(user=request.user)
            .select_related("event", "outcome")
            .order_by("-opened_at")
        ),
    )
    open_positions = [p for p in positions if p.result == "open"]
    settling_positions = [p for p in positions if p.result == "settling"]
    settled_positions = [
        p for p in positions if p.result in ("won", "lost", "void", "claimed", "closed")
    ]
    agent_claimable = [
        p for p in settled_positions if p.claimable and getattr(p, "claim_via_agent", False)
    ]
    all_closed = [
        p for p in settled_positions if not (p.claimable and getattr(p, "claim_via_agent", False))
    ]
    all_recent_trades = list_recent_trades(request.user)
    closed_lookback = parse_closed_lookback(request.GET.get("closed"))
    cutoff = closed_lookback_cutoff(closed_lookback)
    closed_positions = [
        p
        for p in all_closed
        if in_closed_lookback(getattr(p, "settled_at", None) or p.opened_at, cutoff)
    ]
    recent_trades = [
        t
        for t in all_recent_trades
        if in_closed_lookback(t.opened_at, cutoff)
    ]

    total_pnl = sum((p.pnl or Decimal("0")) for p in positions)
    event_ids = {p.event_id for p in positions}
    won_count = sum(1 for p in positions if p.result in ("won", "claimed"))
    lost_count = sum(1 for p in positions if p.result == "lost")

    today = timezone.now().date()
    in_active = Decimal("0")
    potential_payout = Decimal("0")
    today_result = Decimal("0")
    for pos in open_positions:
        cost = (pos.amount or Decimal("0")) * (pos.entry_price or Decimal("0"))
        in_active += cost
        potential_payout += pos.amount or Decimal("0")
        today_result += pos.pnl or Decimal("0")
        annotate_event_display(pos.event)
        pos.you_put_in = cost
        pos.potential_payout = pos.amount or Decimal("0")
    for pos in settling_positions + settled_positions:
        annotate_event_display(pos.event)
        pos.you_put_in = (pos.amount or Decimal("0")) * (pos.entry_price or Decimal("0"))
        pos.potential_payout = pos.amount or Decimal("0")
        settled_at = getattr(pos, "settled_at", None)
        if settled_at and settled_at.date() == today:
            today_result += pos.pnl or Decimal("0")
    for trade in recent_trades:
        annotate_event_display(trade.event)

    available = None
    if wallet_balances:
        available = wallet_balances.get("collateral_balance") if isinstance(wallet_balances, dict) else getattr(wallet_balances, "collateral_balance", None)

    copy_relationships = list(
        CopyRelationship.objects.filter(user=request.user)
        .exclude(status=CopyRelationship.Status.STOPPED)
        .select_related("trader")
        .annotate(
            pending_count=Count(
                "executions",
                filter=Q(executions__status=CopyExecution.Status.PENDING),
            )
        )
        .order_by("-updated_at")
    )
    pending_copy_count = sum(int(getattr(rel, "pending_count", 0) or 0) for rel in copy_relationships)
    agent_can_auto_copy = bool(
        agent and agent.status == DreamAgent.Status.RUNNING
    )

    return render(
        request,
        "portfolio/index.html",
        {
            "total_pnl": total_pnl,
            "today_result": today_result,
            "available": available,
            "in_active_events": in_active,
            "potential_payout": potential_payout,
            "open_positions": open_positions,
            "settling_positions": settling_positions,
            "settled_positions": settled_positions,
            "closed_positions": closed_positions,
            "closed_lookback": closed_lookback,
            "closed_lookbacks": CLOSED_LOOKBACKS,
            "closed_total": len(all_closed),
            "recent_trades_total": len(all_recent_trades),
            "agent_claimable": agent_claimable,
            "recent_trades": recent_trades,
            "has_positions": bool(
                open_positions or settling_positions or settled_positions or recent_trades
            ),
            "event_count": len(event_ids),
            "won_count": won_count,
            "lost_count": lost_count,
            "wallet_balances": wallet_balances,
            "is_authenticated": True,
            "agent": agent,
            "smart_account": smart_account,
            "agent_performance": agent_performance,
            "agent_balance": agent_balance,
            "telegram_link": serialize_link(get_link(request.user)),
            "copy_relationships": copy_relationships,
            "pending_copy_count": pending_copy_count,
            "agent_can_auto_copy": agent_can_auto_copy,
            "grant_health": dream_agent_service.grant_health(request.user),
            "book": get_portfolio_summary(request.user),
        },
    )


def following(request):
    copies = []
    if request.user.is_authenticated:
        copies = list(
            CopyRelationship.objects.filter(user=request.user)
            .exclude(status=CopyRelationship.Status.STOPPED)
            .select_related("trader")
            .annotate(
                copied_count=Count(
                    "executions",
                    filter=Q(
                        executions__status__in=[
                            CopyExecution.Status.PENDING,
                            CopyExecution.Status.APPROVED,
                            CopyExecution.Status.EXECUTED,
                        ]
                    ),
                ),
                approved_count=Count(
                    "executions",
                    filter=Q(
                        executions__status__in=[
                            CopyExecution.Status.APPROVED,
                            CopyExecution.Status.EXECUTED,
                        ]
                    ),
                ),
                skipped_count=Count(
                    "executions",
                    filter=Q(executions__status=CopyExecution.Status.SKIPPED),
                ),
            )
            .order_by("-updated_at")
        )
        for rel in copies:
            rel.is_smart_on = rel.status == CopyRelationship.Status.ACTIVE

    active_traders = list_active_traders()
    followed_trader_ids = {rel.trader_id for rel in copies}
    smart_copy_on_ids = {
        rel.trader_id
        for rel in copies
        if rel.status == CopyRelationship.Status.ACTIVE
    }
    # Templates cannot index a dict by variable, so hang the relationship
    # (and therefore the pk Unfollow needs) directly on each trader row.
    rel_by_trader = {rel.trader_id: rel for rel in copies}
    for trader in active_traders:
        trader.follow_rel = rel_by_trader.get(trader.pk)
    return render(
        request,
        "copy/following.html",
        {
            "copy_relationships": copies,
            "active_traders": active_traders,
            "followed_trader_ids": followed_trader_ids,
            "smart_copy_on_ids": smart_copy_on_ids,
            "is_authenticated": request.user.is_authenticated,
        },
    )


def copy_activity(request):
    if not request.user.is_authenticated:
        return render(
            request,
            "copy/activity.html",
            {"executions": [], "is_authenticated": False},
        )
    executions = (
        CopyExecution.objects.filter(relationship__user=request.user)
        .select_related(
            "relationship__trader",
            "source_trade__event",
            "source_trade__outcome",
            "copied_trade",
        )
        .order_by("-created_at")[:50]
    )
    for ex in executions:
        annotate_event_display(ex.source_trade.event)
    return render(
        request,
        "copy/activity.html",
        {"executions": executions, "is_authenticated": True},
    )


def copy_settings(request, pk: int):
    if not request.user.is_authenticated:
        return render(
            request,
            "copy/settings.html",
            {"relationship": None, "is_authenticated": False},
        )
    rel = get_object_or_404(
        CopyRelationship.objects.select_related("trader"),
        pk=pk,
        user=request.user,
    )
    min_wr = rel.min_win_rate or Decimal("0.65")
    if min_wr <= 1:
        min_wr_pct = int(round(float(min_wr) * 100))
    else:
        min_wr_pct = int(round(float(min_wr)))
    return render(
        request,
        "copy/settings.html",
        {
            "relationship": rel,
            "is_authenticated": True,
            "min_wr_pct": min_wr_pct,
        },
    )


def trader_detail(request, pk: int):
    trader = get_object_or_404(TraderProfile, pk=pk)
    analytics = build_trader_analytics(trader)
    analytics["mix"] = asset_mix_groups(analytics.get("assets") or [])
    analytics["view"] = trader_view_signals(
        win_pct=analytics.get("win_pct"),
        indexed_count=analytics.get("indexed_count") or trader.total_trades,
        last_fill_at=analytics.get("last_fill_at"),
    )
    for fill in analytics.get("fills") or []:
        annotate_event_display(fill.event)
    relationship = None
    already_following = False
    if request.user.is_authenticated:
        relationship = (
            CopyRelationship.objects.filter(user=request.user, trader=trader)
            .exclude(status=CopyRelationship.Status.STOPPED)
            .first()
        )
        already_following = relationship is not None
    return render(
        request,
        "copy/trader_detail.html",
        {
            "trader": trader,
            "analytics": analytics,
            "fills": analytics["fills"],
            "already_following": already_following,
            "relationship": relationship,
            "win_pct": analytics["win_pct"],
            "completed_events": analytics["unique_markets"]
            or trader.completed_trades
            or trader.total_trades,
        },
    )


def dream_agent(request):
    """DreamAgent performance + autonomous status."""
    from apps.agents.models import DreamAgent
    from services import dream_agent_service, smart_account_service
    from services.portfolio_service import list_agent_claimable

    agent = None
    performance = {"agent": None}
    sa = None
    agent_claimable = []
    if request.user.is_authenticated:
        sa = smart_account_service.get_account(request.user)
        agent = (
            DreamAgent.objects.filter(user=request.user)
            .select_related("smart_account")
            .order_by("-updated_at")
            .first()
        )
        try:
            agent_claimable = list_agent_claimable(request.user)
            for pos in agent_claimable:
                annotate_event_display(pos.event)
        except Exception:
            logger.warning("agent claimable list failed", exc_info=True)
            agent_claimable = []
    agent_balance = None
    if sa:
        try:
            agent_balance = smart_account_service.get_balance(sa)
        except Exception:
            logger.warning("agent page balance unavailable", exc_info=True)
            agent_balance = None
    if agent:
        performance = dream_agent_service.agent_performance(agent, balance=agent_balance)
    executions = []
    following_count = 0
    if request.user.is_authenticated:
        following_count = CopyRelationship.objects.filter(
            user=request.user,
            status=CopyRelationship.Status.ACTIVE,
        ).count()
        executions = list(
            CopyExecution.objects.filter(relationship__user=request.user)
            .select_related(
                "relationship__trader",
                "source_trade__event",
                "source_trade__outcome",
                "copied_trade",
            )
            .order_by("-created_at")[:8]
        )
        for ex in executions:
            annotate_event_display(ex.source_trade.event)
    today_result = None
    if performance.get("pnl") is not None:
        today_result = performance.get("pnl")
    health = {}
    if request.user.is_authenticated:
        health = dream_agent_service.grant_health(request.user)
    return render(
        request,
        "agent/index.html",
        {
            "agent": agent,
            "smart_account": sa,
            "performance": performance,
            "agent_balance": agent_balance,
            "is_authenticated": request.user.is_authenticated,
            "executions": executions,
            "following_count": following_count,
            "today_result": today_result,
            "grant_health": health,
            "agent_claimable": agent_claimable,
        },
    )


def dream_agent_activate(request):
    """Activate Dream Agent — grant bounded TRADE_EVENT_CONTRACT permission."""
    from services import smart_account_service

    grant = smart_account_service.grant_payload_for_ui(request.user) if request.user.is_authenticated else {}
    health = {}
    if request.user.is_authenticated:
        from services import dream_agent_service

        health = dream_agent_service.grant_health(request.user)
    return render(
        request,
        "agent/activate.html",
        {
            "grant": grant,
            "grant_health": health,
            "is_authenticated": request.user.is_authenticated,
        },
    )


def dream_agent_skips(request):
    """Why didn't my agent trade?"""
    from apps.agents.models import AgentEvaluation, DreamAgent
    from services import dream_agent_service

    evaluations = []
    agent = None
    if request.user.is_authenticated:
        agent = (
            DreamAgent.objects.filter(user=request.user)
            .order_by("-updated_at")
            .first()
        )
        if agent:
            evaluations = [
                dream_agent_service.serialize_evaluation(ev)
                for ev in AgentEvaluation.objects.filter(
                    agent=agent,
                    decision=AgentEvaluation.Decision.SKIPPED,
                )[:40]
            ]
    return render(
        request,
        "agent/skips.html",
        {
            "agent": agent,
            "evaluations": evaluations,
            "is_authenticated": request.user.is_authenticated,
        },
    )


@require_POST
def api_ai_chat(request):
    """Stub AI chat endpoint for search bar."""
    try:
        body = json.loads(request.body)
    except json.JSONDecodeError:
        body = {}
    query = body.get("query") or body.get("message") or ""
    return JsonResponse(
        {
            "reply": (
                f'DreamLens is analyzing: "{query}". '
                "Try exploring Event Radar on the home page for live signals."
            ),
            "query": query,
        }
    )


