"""DreamDEX data transfer objects."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Literal, TypedDict


OutcomeSide = Literal["YES", "NO"]
OrderSide = Literal["BUY_YES", "SELL_YES", "BUY_NO", "SELL_NO"]
OrderType = Literal["MARKET", "LIMIT", "FILL_OR_KILL", "POST_ONLY"]


@dataclass(frozen=True, slots=True)
class OutcomeDTO:
    outcome_type: OutcomeSide
    token_id: str
    symbol: str
    price: Decimal


@dataclass(frozen=True, slots=True)
class EventDTO:
    market_id: str
    asset: str
    strike: int
    interval_sec: int
    expiry: datetime
    trading_start: datetime
    yes_token_id: str
    no_token_id: str
    yes_price: Decimal
    no_price: Decimal
    cumulative_quote_volume: Decimal
    last_price: Decimal
    venue_id: str
    status: str
    pool_address: str | None = None
    market_address: str | None = None
    collateral: str | None = None
    trade_count: int = 0
    oracle_question_id: str | None = None
    cumulative_base_volume: Decimal = Decimal("0")
    yes_symbol: str = ""
    no_symbol: str = ""
    winning_outcome: OutcomeSide | None = None
    question: str = ""
    opening_price: Decimal | None = None


@dataclass(frozen=True, slots=True)
class OrderBookLevel:
    price: Decimal
    quantity: Decimal


@dataclass(frozen=True, slots=True)
class OrderBookDTO:
    symbol: str
    bids: list[OrderBookLevel]
    asks: list[OrderBookLevel]
    mid_price: Decimal | None = None


@dataclass(frozen=True, slots=True)
class FillDTO:
    id: str
    market_id: str
    pool: str
    fill_price: Decimal
    quantity: Decimal
    quote_quantity: Decimal
    maker: str
    taker: str
    maker_side: OutcomeSide
    taker_side: OutcomeSide
    kind: str
    taker_is_bid: bool
    taker_order: str
    timestamp: datetime
    tx_hash: str
    trader_label: str | None = None
    maker_is_buy: bool = True
    taker_is_buy: bool = True


@dataclass(frozen=True, slots=True)
class CandleDTO:
    timestamp: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal


@dataclass(frozen=True, slots=True)
class OnchainMarketDTO:
    market_id: str
    status: int
    status_label: str
    pool: str
    market_address: str
    outcome_token: str
    yes_id: str
    no_id: str
    expiry: datetime


@dataclass(frozen=True, slots=True)
class PositionDTO:
    market_id: str
    yes_balance: Decimal
    no_balance: Decimal


class OutcomeBalancesDTO(TypedDict):
    yes_balance: Decimal
    no_balance: Decimal


@dataclass(frozen=True, slots=True)
class WalletBalancesDTO:
    """Native gas token + DreamDEX trading collateral for a wallet."""

    address: str
    native_balance: Decimal
    native_symbol: str
    collateral_balance: Decimal
    collateral_symbol: str
    collateral_address: str
    chain_id: int


@dataclass(frozen=True, slots=True)
class TradeIntent:
    market_id: str
    pool: str
    side: OrderSide
    price: Decimal
    quantity: Decimal
    order_type: OrderType = "LIMIT"
    time_in_force: str = "IOC"
    account: str = ""
    expire_timestamp_ns: int | None = None


@dataclass(frozen=True, slots=True)
class UnsignedTxDTO:
    to: str
    data: str
    value: int
    chain_id: int
    description: str = ""
    metadata: dict[str, str] = field(default_factory=dict)
