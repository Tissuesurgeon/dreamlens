"""Suggested traders come from on-chain fills, never mock seed wallets."""

from __future__ import annotations

from decimal import Decimal

import pytest
from django.core.cache import cache

from apps.dreamcopy.models import TraderProfile, TraderTrade
from apps.events.models import EventContract
from integrations.dreamdex.mock import TRADER_WALLETS
from services.trader_service import (
    build_trader_analytics,
    is_onchain_trader_wallet,
    list_active_traders,
    list_suggested_traders,
    purge_seed_traders,
)


@pytest.mark.django_db
def test_seed_wallets_are_not_onchain():
    for address in TRADER_WALLETS.values():
        assert is_onchain_trader_wallet(address) is False
    assert is_onchain_trader_wallet("0x6730d3a2a217108ab53ccfe60ffdad05d3c124e5") is True


@pytest.mark.django_db
def test_purge_and_list_skips_seed_wallets(sample_event):
    cache.clear()
    seed = TraderProfile.objects.create(
        wallet_address=TRADER_WALLETS["AlphaTrader"].lower(),
        display_name="AlphaTrader",
        total_trades=99,
        total_volume=Decimal("999"),
        trader_score=Decimal("0.99"),
    )
    real = TraderProfile.objects.create(
        wallet_address="0x6730d3a2a217108ab53ccfe60ffdad05d3c124e5",
        total_trades=12,
        total_volume=Decimal("65.29"),
        trader_score=Decimal("0.35"),
    )
    TraderTrade.objects.create(
        trader=real,
        event=sample_event,
        outcome=sample_event.outcomes.first(),
        entry_price=Decimal("0.4"),
        amount=Decimal("10"),
        opened_at=sample_event.trading_start or sample_event.expiry_time,
        external_trade_id="fill-real-1",
    )

    removed = purge_seed_traders()
    assert removed >= 1
    assert not TraderProfile.objects.filter(pk=seed.pk).exists()

    suggested = list_suggested_traders(limit=8)
    wallets = {t.wallet_address.lower() for t in suggested}
    assert real.wallet_address in wallets
    assert TRADER_WALLETS["AlphaTrader"].lower() not in wallets


@pytest.mark.django_db
def test_list_active_traders_syncs_markets_when_index_is_empty():
    cache.clear()
    from apps.events.models import EventContract

    assert EventContract.objects.count() == 0
    list_active_traders()
    assert EventContract.objects.filter(
        status__in=[EventContract.Status.TRADING, EventContract.Status.LIVE]
    ).exists()


@pytest.mark.django_db
def test_trader_detail_page_opens(client, sample_event):
    trader = TraderProfile.objects.create(
        wallet_address="0x6730d3a2a217108ab53ccfe60ffdad05d3c124e5",
        total_trades=4,
        total_volume=Decimal("12.5"),
    )
    TraderTrade.objects.create(
        trader=trader,
        event=sample_event,
        outcome=sample_event.outcomes.first(),
        entry_price=Decimal("0.4"),
        amount=Decimal("3"),
        opened_at=sample_event.expiry_time,
        external_trade_id="fill-view-1",
    )
    from services.event_copy import event_question

    res = client.get(f"/traders/{trader.pk}/")
    assert res.status_code == 200
    assert trader.wallet_address.encode() in res.content
    assert event_question(sample_event).encode() in res.content
    assert b"What they usually trade" in res.content
    assert b"How DreamLens sees them" in res.content
    assert b"On-chain fills" in res.content
    assert b"Smart Copy" in res.content


@pytest.mark.django_db
def test_list_active_traders_returns_all_onchain(sample_event):
    cache.clear()
    created = []
    for i in range(10):
        trader = TraderProfile.objects.create(
            wallet_address=f"0x{i + 1:040x}",
            total_trades=i + 1,
            total_volume=Decimal(str(10 + i)),
            trader_score=Decimal("0.40"),
        )
        TraderTrade.objects.create(
            trader=trader,
            event=sample_event,
            outcome=sample_event.outcomes.first(),
            entry_price=Decimal("0.4"),
            amount=Decimal("2"),
            opened_at=sample_event.expiry_time,
            external_trade_id=f"fill-all-{i}",
        )
        created.append(trader)

    traders = list_active_traders()
    wallets = {t.wallet_address.lower() for t in traders}
    assert len(traders) >= 10
    for trader in created:
        assert trader.wallet_address in wallets


@pytest.mark.django_db
def test_following_page_lists_active_traders_with_copy_actions(client, sample_event):
    cache.clear()
    trader = TraderProfile.objects.create(
        wallet_address="0x6730d3a2a217108ab53ccfe60ffdad05d3c124e5",
        total_trades=6,
        total_volume=Decimal("40"),
        trader_score=Decimal("0.42"),
    )
    TraderTrade.objects.create(
        trader=trader,
        event=sample_event,
        outcome=sample_event.outcomes.first(),
        entry_price=Decimal("0.5"),
        amount=Decimal("4"),
        opened_at=sample_event.expiry_time,
        external_trade_id="fill-follow-list-1",
    )
    res = client.get("/following/")
    assert res.status_code == 200
    assert b"Smart Copy" in res.content
    assert b"/traders/%d/" % trader.pk in res.content
    assert b"data-follow-trader" in res.content
    assert b"data-open-smart-copy" in res.content
    assert b"data-trader-id=\"%d\"" % trader.pk in res.content
    assert b"follow-wallet-form" in res.content
    assert b"Paste any DreamDEX wallet" in res.content


@pytest.mark.django_db
def test_trader_analytics_splits_yes_no(sample_event):
    trader = TraderProfile.objects.create(
        wallet_address="0x6730d3a2a217108ab53ccfe60ffdad05d3c124e5",
        total_trades=2,
        total_volume=Decimal("5"),
    )
    yes = sample_event.outcomes.get(outcome_type="YES")
    no = sample_event.outcomes.get(outcome_type="NO")
    TraderTrade.objects.create(
        trader=trader,
        event=sample_event,
        outcome=yes,
        entry_price=Decimal("0.5"),
        amount=Decimal("10"),
        opened_at=sample_event.expiry_time,
        external_trade_id="an-yes",
    )
    TraderTrade.objects.create(
        trader=trader,
        event=sample_event,
        outcome=no,
        entry_price=Decimal("0.5"),
        amount=Decimal("2"),
        opened_at=sample_event.expiry_time,
        external_trade_id="an-no",
    )
    analytics = build_trader_analytics(trader)
    assert analytics["yes_count"] == 1
    assert analytics["no_count"] == 1
    assert analytics["unique_markets"] == 1
    assert analytics["indexed_count"] == 2
    assert analytics["chart"]["labels"]

