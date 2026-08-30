"""Trading state machine and prepare/confirm flows."""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

import pytest
from django.utils import timezone

from apps.trading.models import Trade
from integrations.dreamdex.exceptions import DreamDEXNotFound
from services.trading_service import TradingError, confirm_trade, prepare_sell_trade, prepare_trade


@pytest.mark.django_db
def test_trade_state_machine_transitions(user, sample_event, wallet):
    trade, _unsigned, _approval = prepare_trade(
        user=user,
        event_id=sample_event.pk,
        outcome="YES",
        amount=Decimal("10"),
        wallet_address=wallet.address,
    )
    assert trade.status == Trade.Status.AWAITING_CONFIRMATION

    confirmed = confirm_trade(trade.pk, tx_hash="0x" + "ab" * 32, user=user)
    assert confirmed.status == Trade.Status.CONFIRMED
    assert confirmed.transaction_hash.startswith("0x")
    assert confirmed.settled_at is not None


@pytest.mark.django_db
def test_prepare_trade_payload_uses_hex_chain_id_for_wallet(user, sample_event, wallet, settings):
    """MetaMask eth_sendTransaction rejects decimal chainId (50312); it needs 0xc488."""
    trade, unsigned, _approval = prepare_trade(
        user=user,
        event_id=sample_event.pk,
        outcome="YES",
        amount=Decimal("10"),
        wallet_address=wallet.address,
    )
    payload = trade.metadata_json["unsigned_tx"]
    assert payload["chain_id"] == unsigned.chain_id == settings.DREAMDEX_CHAIN_ID
    assert payload["chainId"] == hex(int(settings.DREAMDEX_CHAIN_ID))
    assert payload["chainId"].startswith("0x")
    assert payload["to"]
    assert payload["data"].startswith("0x")
    assert payload["gas"] == "0x3d090"
    assert payload["maxFeePerGas"] == "0x2cb417800"
    assert payload["maxPriorityFeePerGas"] == "0x3b9aca00"
    # Frontend omits chainId on eth_sendTransaction; MetaMask uses the selected network.


@pytest.mark.django_db
def test_prepare_trade_api_returns_hex_chain_id(client, user, sample_event, wallet, settings):
    client.force_login(user)
    res = client.post(
        "/api/trades/prepare/",
        data={
            "event_id": sample_event.pk,
            "outcome": "YES",
            "amount": "10",
            "wallet_address": wallet.address,
        },
        content_type="application/json",
    )
    assert res.status_code == 201
    tx = res.json()["unsigned_tx"]
    assert tx["chainId"] == hex(int(settings.DREAMDEX_CHAIN_ID))
    assert tx["to"]
    assert tx["data"].startswith("0x")
    assert tx["gas"] == "0x3d090"
    assert tx["maxFeePerGas"] == "0x2cb417800"
    assert tx["maxPriorityFeePerGas"] == "0x3b9aca00"


@pytest.mark.django_db
def test_fake_external_id_cannot_be_executed(user, fake_external_event, wallet):
    with pytest.raises(DreamDEXNotFound):
        prepare_trade(
            user=user,
            event_id=fake_external_event.pk,
            outcome="YES",
            amount=Decimal("5"),
            wallet_address=wallet.address,
        )


@pytest.mark.django_db
def test_expired_event_prepare_raises(user, expired_event, wallet):
    with pytest.raises(TradingError, match="expired"):
        prepare_trade(
            user=user,
            event_id=expired_event.pk,
            outcome="YES",
            amount=Decimal("5"),
            wallet_address=wallet.address,
        )


@pytest.mark.django_db
def test_prepare_sell_creates_sell_trade(user, sample_event, wallet):
    trade, unsigned, _approval = prepare_sell_trade(
        user=user,
        event_id=sample_event.pk,
        outcome="YES",
        quantity=Decimal("20"),
        wallet_address=wallet.address,
    )
    assert trade.side == Trade.Side.SELL
    assert trade.status == Trade.Status.AWAITING_CONFIRMATION
    assert trade.amount == Decimal("20")
    assert unsigned.data.startswith("0x")
    payload = trade.metadata_json["unsigned_tx"]
    assert payload["chainId"].startswith("0x")
    assert payload["metadata"]["side"] == "SELL_YES"


@pytest.mark.django_db
def test_market_about_to_lock_prepare_raises(user, sample_event, wallet):
    sample_event.expiry_time = timezone.now() + timedelta(seconds=10)
    sample_event.save(update_fields=["expiry_time"])
    with pytest.raises(TradingError, match="about to lock"):
        prepare_trade(
            user=user,
            event_id=sample_event.pk,
            outcome="YES",
            amount=Decimal("5"),
            wallet_address=wallet.address,
        )
