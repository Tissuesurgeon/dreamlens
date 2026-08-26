"""Thin wrappers around the DreamDEX adapter for trading operations."""

from __future__ import annotations

from integrations.dreamdex.adapter import get_adapter
from integrations.dreamdex.types import (
    CandleDTO,
    OrderBookDTO,
    TradeIntent,
    UnsignedTxDTO,
)


def get_order_book(yes_symbol: str, depth: int = 5) -> OrderBookDTO:
    return get_adapter().get_order_book(yes_symbol, depth)


def get_candles(pool: str, interval: int) -> list[CandleDTO]:
    return get_adapter().get_candles(pool, interval)


def prepare_place_order(intent: TradeIntent) -> UnsignedTxDTO:
    return get_adapter().prepare_place_order(intent)


def prepare_redeem(
    *,
    market_id: str,
    account: str,
    outcome_idx: int,
    amount: int,
) -> UnsignedTxDTO:
    return get_adapter().prepare_redeem(
        market_id=market_id,
        account=account,
        outcome_idx=outcome_idx,
        amount=amount,
    )
