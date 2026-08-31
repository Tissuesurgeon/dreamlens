"""First-session state: Connect → trading account → fund → allow → first trade."""

from __future__ import annotations

from typing import Any

from apps.agents.models import SmartAccount
from apps.trading.models import Trade
from services.dream_agent_service import get_tradable_agent
from services.event_copy import SIDE_BEGINNER
from services.smart_account_service import get_account

# Official Shannon faucets (testnet only — no real monetary value).
STT_FAUCET_URL = "https://testnet.somnia.network/"
TEST_USDC_FAUCET_URL = "https://testnet.somnia.network/"

STEP_CONNECT = "connect"
STEP_CREATE = "create"
STEP_FUND = "fund"
STEP_ALLOW = "allow"
STEP_TRADE = "trade"
STEP_DONE = "done"

STEPS = (STEP_CONNECT, STEP_CREATE, STEP_FUND, STEP_ALLOW, STEP_TRADE, STEP_DONE)

STEP_COPY = {
    STEP_CONNECT: {
        "title": "Connect MetaMask",
        "why": "Your wallet owns the trading account. DreamLens never holds the keys.",
        "cta": "Connect MetaMask",
    },
    STEP_CREATE: {
        "title": "Create your trading account",
        "why": "Trades, Telegram, and Smart Copy all use this account — not your main wallet.",
        "cta": "Create your trading account",
    },
    STEP_FUND: {
        "title": "Add money",
        "why": "Trading dollars go in first. A small network fee keeps DreamLens able to place trades.",
        "cta": "Add trading dollars",
    },
    STEP_ALLOW: {
        "title": "Allow DreamLens to trade for you",
        "why": "One signature. DreamLens can buy and sell Event Contracts within your limits. It cannot withdraw.",
        "cta": "Allow DreamLens to trade",
    },
    STEP_TRADE: {
        "title": "Place a $1 YES or NO",
        "why": SIDE_BEGINNER,
        "cta": "Choose YES or NO",
    },
    STEP_DONE: {
        "title": "You’re in. This trade is open.",
        "why": "When the event ends you claim winnings. Close is selling early — not the same as claim.",
        "cta": "See your trade",
    },
}


def _has_smart_account_trade(user, sa: SmartAccount | None) -> bool:
    if sa is None:
        return False
    addr = (sa.address or "").strip().lower()
    if not addr:
        return False
    confirmed = (
        Trade.objects.filter(
            user=user,
            status__in=(Trade.Status.CONFIRMED, Trade.Status.SUBMITTED),
        )
        .exclude(transaction_hash="")
    )
    for trade in confirmed.only("metadata_json")[:40]:
        meta = trade.metadata_json or {}
        wallet = str(meta.get("wallet") or meta.get("wallet_address") or "").lower()
        if wallet == addr:
            return True
        if str(meta.get("smart_account") or "").lower() == addr:
            return True
        if str(meta.get("source") or "").lower() in {"telegram", "web", "claim"}:
            if wallet == addr:
                return True
    return False


def first_session_state(user) -> dict[str, Any]:
    """Where a beginner is in Connect → first Smart Account trade."""
    authenticated = bool(user and getattr(user, "is_authenticated", False))
    sa = get_account(user) if authenticated else None
    funded = bool(sa and sa.status == SmartAccount.Status.FUNDED)
    can_trade = bool(authenticated and get_tradable_agent(user))
    has_first_trade = _has_smart_account_trade(user, sa) if authenticated else False

    if not authenticated:
        step = STEP_CONNECT
    elif sa is None:
        step = STEP_CREATE
    elif not funded:
        step = STEP_FUND
    elif not can_trade:
        step = STEP_ALLOW
    elif not has_first_trade:
        step = STEP_TRADE
    else:
        step = STEP_DONE

    copy = STEP_COPY[step]
    # Quiet the app desk only for signed-in users who have not finished the first trade.
    incomplete = authenticated and step != STEP_DONE
    return {
        "step": step,
        "step_index": STEPS.index(step) + 1,
        "step_count": 5,
        "incomplete": incomplete,
        "authenticated": authenticated,
        "has_account": sa is not None,
        "account_address": sa.address if sa else "",
        "funded": funded,
        "can_trade": can_trade,
        "has_first_trade": has_first_trade,
        "title": copy["title"],
        "why": copy["why"],
        "cta": copy["cta"],
        "stt_faucet": STT_FAUCET_URL,
        "usdc_faucet": TEST_USDC_FAUCET_URL,
    }
