"""Event volume, book liquidity, and USD formatting."""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

import pytest
from django.utils import timezone

from apps.core.templatetags.dreamlens_extras import usd_amount
from services.event_copy import format_collateral
from apps.dreamcopy.models import TraderProfile
from integrations.dreamdex.types import FillDTO
from services.market_stats import book_liquidity, event_market_stats, traded_volume

TAKER = "0x470346239a34687d9ad98f85002240ab3a659fe2"
MAKER = "0x6730d3a2a217108ab53ccfe60ffdad05d3c124e5"
OUTSIDER = "0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa1"


def _seed_event_fill(
    mock_adapter,
    event,
    *,
    maker: str,
    taker: str,
    quantity: Decimal = Decimal("20"),
    price: Decimal = Decimal("0.72"),
    market_id: str | None = None,
    fill_id: str = "fill-event-pos-1",
) -> FillDTO:
    pool = event.pool_address
    fill = FillDTO(
        id=fill_id,
        market_id=market_id or event.external_id,
        pool=pool,
        fill_price=price,
        quantity=quantity,
        quote_quantity=price * quantity,
        maker=maker,
        taker=taker,
        maker_side="YES",
        taker_side="YES",
        kind="DIRECT_YES",
        taker_is_bid=True,
        taker_order="order-1",
        timestamp=timezone.now() - timedelta(minutes=3),
        tx_hash="0x" + "ab" * 32,
        maker_is_buy=False,
        taker_is_buy=True,
    )
    mock_adapter._fills.setdefault(pool, []).append(fill)
    return fill


def test_usd_amount_keeps_cents_under_1000():
    assert usd_amount(Decimal("14.44")) == "$14.44"
    assert usd_amount(Decimal("0.50")) == "$0.50"
    assert usd_amount(Decimal("1234.5")) == "$1,235"
    assert format_collateral(Decimal("5"), compact=True) == "$5"


@pytest.mark.django_db
def test_book_liquidity_uses_order_book_not_volume_heuristic(sample_event, mock_adapter):
    sample_event.cumulative_quote_volume = Decimal("100000")
    sample_event.save(update_fields=["cumulative_quote_volume"])
    liq = book_liquidity(sample_event)
    assert liq > 0
    assert liq != (Decimal("100000") / Decimal("1000"))
    assert liq != (Decimal("100000") / Decimal("10"))


@pytest.mark.django_db
def test_traded_volume_falls_back_to_fills(sample_event, mock_adapter):
    sample_event.cumulative_quote_volume = Decimal("0")
    sample_event.trade_count = 0
    sample_event.save(update_fields=["cumulative_quote_volume", "trade_count"])
    vol = traded_volume(sample_event)
    assert vol > 0
    stats = event_market_stats(sample_event)
    assert stats["volume"] == vol
    assert stats["trade_count"] >= 1
    assert stats["liquidity"] > 0


@pytest.mark.django_db
def test_explore_shows_volume_and_trade_count(client, sample_event, mock_adapter):
    sample_event.cumulative_quote_volume = Decimal("14.44")
    sample_event.trade_count = 3
    sample_event.save(update_fields=["cumulative_quote_volume", "trade_count"])
    res = client.get("/discover/")
    assert res.status_code == 200
    body = res.content.decode()
    assert "$14.44" in body
    assert "3 trade" in body
    assert "Discover" in body
    assert 'id="ai-search-form"' not in body


@pytest.mark.django_db
def test_event_stats_ignore_global_traders_and_other_markets(sample_event, mock_adapter):
    TraderProfile.objects.create(
        wallet_address=OUTSIDER,
        total_trades=99,
        total_volume=Decimal("99999"),
    )
    _seed_event_fill(mock_adapter, sample_event, maker=MAKER, taker=TAKER)
    _seed_event_fill(
        mock_adapter,
        sample_event,
        maker=OUTSIDER,
        taker=OUTSIDER,
        market_id="0x" + "cd" * 32,
        fill_id="fill-other-market",
        quantity=Decimal("500"),
    )
    stats = event_market_stats(sample_event)
    wallets = {row["wallet"] for row in stats["traders"]}
    assert stats["trader_count"] == 2
    assert TAKER in wallets
    assert MAKER in wallets
    assert OUTSIDER not in wallets
    taker_row = next(row for row in stats["traders"] if row["wallet"] == TAKER)
    assert taker_row["position_label"] == "20 YES"
    assert taker_row["position_amount"] > 0
    maker_row = next(row for row in stats["traders"] if row["wallet"] == MAKER)
    assert "short" in maker_row["position_label"]


@pytest.mark.django_db
def test_event_page_shows_liquidity_traders_and_positions(client, sample_event, mock_adapter):
    _seed_event_fill(mock_adapter, sample_event, maker=MAKER, taker=TAKER)
    res = client.get(f"/events/{sample_event.pk}/")
    assert res.status_code == 200
    body = res.content.decode()
    assert "Liquidity" in body
    assert "Traders" in body
    assert "20 YES" in body
    assert TAKER[:6] in body
    assert MAKER[:6] in body
    assert "Book</span>" not in body
