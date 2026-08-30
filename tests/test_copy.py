"""DreamCopy execution tests."""

from __future__ import annotations

from decimal import Decimal
from unittest.mock import patch

import pytest

from apps.dreamcopy.models import CopyExecution, CopyRelationship, TraderProfile
from services.copy_service import CopyError, create_copy_relationship, detect_and_process_copy


@pytest.mark.django_db
def test_duplicate_source_trade_does_not_create_duplicate_copy_execution(
    copy_relationship,
    source_trade,
    wallet,
):
    with patch("services.copy_service.evaluate_copy_score") as mock_score:
        from services.copy_score import CopyScoreResult

        mock_score.return_value = CopyScoreResult(
            decision="COPY",
            overall=80,
            confidence=Decimal("0.75"),
            pillars={},
            why=["Mock approves copy"],
            risks=[],
            skip_reasons=[],
            liquidity=Decimal("5000"),
        )
        first = detect_and_process_copy(source_trade)
        second = detect_and_process_copy(source_trade)

    assert len(first) == 1
    assert len(second) == 0
    assert CopyExecution.objects.filter(source_trade=source_trade).count() == 1


PASTE_WALLET = "0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"


@pytest.mark.django_db
def test_follow_by_wallet_creates_stub_profile(user, wallet):
    rel = create_copy_relationship(
        user,
        {
            "wallet_address": "0xAaAaAaAaAaAaAaAaAaAaAaAaAaAaAaAaAaAaAaAa",
            "copy_mode": CopyRelationship.CopyMode.SMART,
            "auto_execute": False,
        },
    )
    trader = TraderProfile.objects.get(wallet_address=PASTE_WALLET)
    assert rel.trader_id == trader.pk
    assert rel.status == CopyRelationship.Status.ACTIVE
    assert trader.total_trades == 0


@pytest.mark.django_db
def test_follow_by_wallet_reuses_existing_trader(user, wallet, trader):
    trader.wallet_address = PASTE_WALLET
    trader.save(update_fields=["wallet_address"])
    rel = create_copy_relationship(user, {"wallet_address": PASTE_WALLET})
    assert rel.trader_id == trader.pk
    assert TraderProfile.objects.filter(wallet_address=PASTE_WALLET).count() == 1


@pytest.mark.django_db
def test_cannot_follow_own_wallet(user, settings):
    from apps.accounts.models import Wallet

    addr = "0xbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
    Wallet.objects.create(
        user=user,
        address=addr,
        chain_id=settings.DREAMDEX_CHAIN_ID,
        is_primary=True,
    )
    with pytest.raises(CopyError, match="own wallet"):
        create_copy_relationship(user, {"wallet_address": addr})


@pytest.mark.django_db
def test_follow_by_wallet_rejects_invalid_address(user, wallet):
    with pytest.raises(CopyError, match="valid 0x"):
        create_copy_relationship(user, {"wallet_address": "not-an-address"})


@pytest.mark.django_db
def test_copy_api_accepts_wallet_address(client, user, wallet):
    client.force_login(user)
    res = client.post(
        "/api/copy/",
        data={
            "wallet_address": PASTE_WALLET,
            "copy_mode": "SMART",
            "auto_execute": False,
        },
        content_type="application/json",
    )
    assert res.status_code == 201
    body = res.json()
    assert body["trader"]["wallet_address"] == PASTE_WALLET
    assert CopyRelationship.objects.filter(user=user, trader__wallet_address=PASTE_WALLET).exists()
