"""Shared pytest fixtures for DreamLens."""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

import pytest
from django.utils import timezone

from apps.accounts.models import User, Wallet
from apps.dreamcopy.models import CopyRelationship, TraderProfile, TraderTrade
from apps.events.models import EventContract, EventOutcome
from integrations.dreamdex.adapter import get_adapter, reset_adapter
from integrations.dreamdex.mock import MockDreamDEXAdapter


@pytest.fixture(autouse=True)
def _force_mock_adapter(settings):
    """Tests always use the in-memory mock adapter, never live indexer/RPC."""
    settings.MOCK_DREAMDEX = True
    settings.MOCK_SMART_ACCOUNT = True
    settings.LOCAL_LLM_ENABLED = False
    settings.LLM_API_KEY = ""
    settings.GEMINI_API_KEY = ""
    settings.OPENROUTER_API_KEY = ""
    settings.CURSOR_API_KEY = ""
    reset_adapter()
    yield
    reset_adapter()


@pytest.fixture
def mock_adapter(settings) -> MockDreamDEXAdapter:
    settings.MOCK_DREAMDEX = True
    reset_adapter()
    return get_adapter()


@pytest.fixture
def user(db) -> User:
    return User.objects.create_user(
        username="demo",
        email="demo@dreamlens.test",
        password="demo-pass-123",
    )


@pytest.fixture
def wallet(user, settings) -> Wallet:
    return Wallet.objects.create(
        user=user,
        address="0xDemo0000000000000000000000000000000001",
        chain_id=settings.DREAMDEX_CHAIN_ID,
        is_primary=True,
    )


@pytest.fixture
def sample_event(db, mock_adapter) -> EventContract:
    """Active event synced from the mock adapter."""
    dto = mock_adapter.list_events(status="live")[0]
    expiry = timezone.now() + timedelta(minutes=30)
    contract = EventContract.objects.create(
        external_id=dto.market_id,
        title=f"{dto.asset} Up/Down · demo",
        underlying_asset=dto.asset,
        status=EventContract.Status.TRADING,
        expiry_time=expiry,
        trading_start=expiry - timedelta(seconds=dto.interval_sec),
        yes_identifier=dto.yes_token_id,
        no_identifier=dto.no_token_id,
        yes_symbol=dto.yes_symbol,
        no_symbol=dto.no_symbol,
        pool_address=dto.pool_address or "",
        market_address=dto.market_address or "",
        venue_id=dto.venue_id,
        strike=dto.strike,
        interval_sec=dto.interval_sec,
        cumulative_quote_volume=dto.cumulative_quote_volume,
        last_price=dto.last_price,
        trade_count=dto.trade_count,
    )
    yes_outcome = EventOutcome.objects.create(
        event=contract,
        outcome_type=EventOutcome.OutcomeType.YES,
        external_identifier=dto.yes_token_id,
        symbol=dto.yes_symbol,
        current_price=dto.yes_price,
    )
    EventOutcome.objects.create(
        event=contract,
        outcome_type=EventOutcome.OutcomeType.NO,
        external_identifier=dto.no_token_id,
        symbol=dto.no_symbol,
        current_price=dto.no_price,
    )
    contract._yes_outcome = yes_outcome  # noqa: SLF001 — test helper
    return contract


@pytest.fixture
def expired_event(db) -> EventContract:
    contract = EventContract.objects.create(
        external_id="0xexpired00000000000000000000000000000001",
        title="Expired BTC event",
        underlying_asset="BTC",
        status=EventContract.Status.TRADING,
        expiry_time=timezone.now() - timedelta(minutes=5),
        yes_identifier="yes-expired",
        no_identifier="no-expired",
        pool_address="0xpool_expired",
    )
    EventOutcome.objects.create(
        event=contract,
        outcome_type=EventOutcome.OutcomeType.YES,
        external_identifier="yes-expired",
        current_price=Decimal("0.50"),
    )
    EventOutcome.objects.create(
        event=contract,
        outcome_type=EventOutcome.OutcomeType.NO,
        external_identifier="no-expired",
        current_price=Decimal("0.50"),
    )
    return contract


@pytest.fixture
def fake_external_event(db) -> EventContract:
    """Event row with an external_id unknown to the mock adapter."""
    contract = EventContract.objects.create(
        external_id="0xfake0000000000000000000000000000000001",
        title="Fake external event",
        underlying_asset="BTC",
        status=EventContract.Status.TRADING,
        expiry_time=timezone.now() + timedelta(minutes=20),
        yes_identifier="yes-fake",
        no_identifier="no-fake",
        pool_address="0xpool_fake",
    )
    EventOutcome.objects.create(
        event=contract,
        outcome_type=EventOutcome.OutcomeType.YES,
        external_identifier="yes-fake",
        current_price=Decimal("0.55"),
    )
    EventOutcome.objects.create(
        event=contract,
        outcome_type=EventOutcome.OutcomeType.NO,
        external_identifier="no-fake",
        current_price=Decimal("0.45"),
    )
    return contract


@pytest.fixture
def trader(db) -> TraderProfile:
    return TraderProfile.objects.create(
        wallet_address="0xAlpha000000000000000000000000000000000001",
        display_name="AlphaTrader",
        completed_trades=20,
        trader_score=Decimal("0.72"),
        total_trades=25,
    )


@pytest.fixture
def copy_relationship(user, trader, wallet) -> CopyRelationship:
    return CopyRelationship.objects.create(
        user=user,
        trader=trader,
        status=CopyRelationship.Status.ACTIVE,
        copy_mode=CopyRelationship.CopyMode.SMART,
        max_per_trade=Decimal("50"),
        max_daily=Decimal("200"),
        minimum_confidence=Decimal("0.55"),
        allowed_assets_json=["BTC", "ETH"],
    )


@pytest.fixture
def source_trade(trader, sample_event) -> TraderTrade:
    yes = sample_event.outcomes.get(outcome_type=EventOutcome.OutcomeType.YES)
    return TraderTrade.objects.create(
        trader=trader,
        event=sample_event,
        outcome=yes,
        entry_price=yes.current_price,
        amount=Decimal("25"),
        opened_at=timezone.now(),
    )
