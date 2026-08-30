"""DreamDEX adapter protocol and factory."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from django.conf import settings

from integrations.dreamdex.types import (
    CandleDTO,
    EventDTO,
    FillDTO,
    OnchainMarketDTO,
    OrderBookDTO,
    OutcomeBalancesDTO,
    TradeIntent,
    UnsignedTxDTO,
    WalletBalancesDTO,
)


@runtime_checkable
class DreamDEXAdapterProtocol(Protocol):
    def list_events(
        self,
        *,
        venue_id: str | None = None,
        status: str | None = None,
    ) -> list[EventDTO]: ...

    def get_event(self, market_id: str) -> EventDTO: ...

    def get_market_onchain(self, market_id: str) -> OnchainMarketDTO: ...

    def get_order_book(self, yes_symbol: str, depth: int = 5) -> OrderBookDTO: ...

    def get_candles(self, pool: str, interval: int) -> list[CandleDTO]: ...

    def get_fills(
        self,
        pool: str,
        *,
        since: int | None = None,
        market_id: str | None = None,
    ) -> list[FillDTO]: ...

    def get_user_fills(
        self,
        account: str,
        *,
        pool: str | None = None,
    ) -> list[FillDTO]: ...

    def get_recent_fills(self, *, limit: int = 300) -> list[FillDTO]: ...

    def get_outcome_balances(self, account: str, market_id: str) -> OutcomeBalancesDTO: ...

    def get_wallet_balances(self, account: str) -> WalletBalancesDTO: ...

    def prepare_place_order(self, intent: TradeIntent) -> UnsignedTxDTO: ...

    def list_finalized_events(
        self,
        *,
        venue_id: str | None = None,
    ) -> list[EventDTO]: ...

    def prepare_redeem(
        self,
        *,
        market_id: str,
        account: str,
        outcome_idx: int,
        amount: int,
    ) -> UnsignedTxDTO: ...

    def prepare_outcome_operator_approval(
        self, *, account: str, spender: str | None = None
    ) -> UnsignedTxDTO | None: ...


_adapter: DreamDEXAdapterProtocol | None = None


def get_adapter() -> DreamDEXAdapterProtocol:
    """Return the configured DreamDEX adapter (mock or live)."""
    global _adapter
    if _adapter is not None:
        return _adapter

    if settings.MOCK_DREAMDEX:
        from integrations.dreamdex.mock import MockDreamDEXAdapter

        _adapter = MockDreamDEXAdapter()
    else:
        from integrations.dreamdex.client import LiveDreamDEXClient

        _adapter = LiveDreamDEXClient()

    return _adapter


def reset_adapter() -> None:
    """Clear cached adapter instance (useful in tests)."""
    global _adapter
    _adapter = None
