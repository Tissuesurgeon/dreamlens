"""First-session state: Connect → trading account → fund → allow → first trade."""

from __future__ import annotations

from typing import Any

from apps.agents.models import SmartAccount
from apps.trading.models import Trade
from services.dream_agent_service import get_tradable_agent
from services.event_copy import SIDE_BEGINNER, event_question, format_collateral
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

# Short names for the five setup steps; "done" is the landing.
STEP_LABELS = {
    STEP_CONNECT: "Connect",
    STEP_CREATE: "Trading account",
    STEP_FUND: "Add money",
    STEP_ALLOW: "Allow",
    STEP_TRADE: "First $1",
}

# Where each step actually happens in the app. The /start/ page only explains;
# the controls live on these pages.
STEP_WHERE = {
    STEP_CONNECT: "Use the Connect button in the top bar. MetaMask opens once; DreamLens only reads your address.",
    STEP_CREATE: "On the Dream Agent page, confirm once in MetaMask. The account is a smart account that your wallet owns.",
    STEP_FUND: "Still on the Dream Agent page: send test dollars, then a small amount of STT for network fees. Both come from the Somnia faucet.",
    STEP_ALLOW: "Sign one permission with your limits: max per trade, max per day, minimum DreamLens Score, expiry. You can pause or revoke any time.",
    STEP_TRADE: "Open Discover, pick a live Event Contract and buy $1 of YES or NO. DreamLens places it from your trading account — no MetaMask popup.",
}


def setup_guide() -> list[dict[str, Any]]:
    """Static, ordered explanation of the five setup steps for the info page."""
    return [
        {
            "n": n,
            "key": key,
            "label": STEP_LABELS[key],
            "title": STEP_COPY[key]["title"],
            "why": STEP_COPY[key]["why"],
            "where": STEP_WHERE[key],
        }
        for n, key in enumerate(STEPS[:-1], start=1)
    ]


def next_step_url(step: str) -> str:
    """Where a signed-in user should go to finish the step they are on."""
    if step == STEP_TRADE:
        return "/discover/"
    if step == STEP_DONE:
        return "/portfolio/"
    return "/agent/activate/"

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
        "title": "Your first ticket is live.",
        "why": "It's on the book until this window ends — or you sell it.",
        "cta": "See your trade",
    },
}


def _trade_on_account(trade: Trade, addr: str) -> bool:
    meta = trade.metadata_json or {}
    wallet = str(meta.get("wallet") or meta.get("wallet_address") or "").lower()
    if wallet == addr:
        return True
    if str(meta.get("smart_account") or "").lower() == addr:
        return True
    return False


def _first_sa_trade(user, sa: SmartAccount | None) -> Trade | None:
    if sa is None:
        return None
    addr = (sa.address or "").strip().lower()
    if not addr:
        return None
    confirmed = (
        Trade.objects.filter(
            user=user,
            status__in=(Trade.Status.CONFIRMED, Trade.Status.SUBMITTED),
        )
        .exclude(transaction_hash="")
        .select_related("event", "outcome")
        .order_by("opened_at")
    )
    for trade in confirmed[:40]:
        if _trade_on_account(trade, addr):
            return trade
    return None


def first_session_state(user) -> dict[str, Any]:
    """Where a beginner is in Connect → first Smart Account trade."""
    authenticated = bool(user and getattr(user, "is_authenticated", False))
    sa = get_account(user) if authenticated else None
    funded = bool(sa and sa.status == SmartAccount.Status.FUNDED)
    can_trade = bool(authenticated and get_tradable_agent(user))
    first_trade = _first_sa_trade(user, sa) if authenticated else None
    has_first_trade = first_trade is not None

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

    copy = dict(STEP_COPY[step])
    if step == STEP_DONE and first_trade is not None:
        side = first_trade.outcome.outcome_type if first_trade.outcome_id else "YES"
        paid = (first_trade.amount or 0) * (first_trade.entry_price or 0)
        copy["title"] = f"You bought {side}."
        copy["why"] = (
            f"{event_question(first_trade.event)} · {format_collateral(paid)} "
            f"at {format_collateral(first_trade.entry_price)}."
        )
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
        "first_trade_side": (
            first_trade.outcome.outcome_type
            if first_trade is not None and first_trade.outcome_id
            else ""
        ),
        "title": copy["title"],
        "why": copy["why"],
        "cta": copy["cta"],
        "next_url": next_step_url(step),
        "stt_faucet": STT_FAUCET_URL,
        "usdc_faucet": TEST_USDC_FAUCET_URL,
    }
