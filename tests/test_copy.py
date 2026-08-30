"""DreamCopy execution tests."""

from __future__ import annotations

from decimal import Decimal
from unittest.mock import patch

import pytest

from apps.dreamcopy.models import CopyExecution, CopyRelationship, TraderProfile
from apps.notifications.models import Notification
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


def _approve_score():
    from services.copy_score import CopyScoreResult

    return CopyScoreResult(
        decision="COPY",
        overall=80,
        confidence=Decimal("0.75"),
        pillars={},
        why=["Mock approves copy"],
        risks=[],
        skip_reasons=[],
        liquidity=Decimal("5000"),
    )


@pytest.mark.django_db
def test_notify_mode_does_not_auto_copy_when_agent_is_running(
    copy_relationship, source_trade
):
    copy_relationship.auto_execute = False
    copy_relationship.min_win_rate = Decimal("0")
    copy_relationship.min_completed_events = 0
    copy_relationship.save(
        update_fields=["auto_execute", "min_win_rate", "min_completed_events"]
    )

    with (
        patch("services.copy_service.evaluate_copy_score", return_value=_approve_score()),
        patch("services.dream_agent_service.get_running_agent", return_value=object()),
        patch("services.dream_agent_service.evaluate_and_maybe_execute") as mock_exec,
    ):
        rows = detect_and_process_copy(source_trade)

    mock_exec.assert_not_called()
    assert len(rows) == 1
    assert rows[0].status == CopyExecution.Status.PENDING
    note = Notification.objects.get(user=copy_relationship.user, kind="copy_pending")
    assert note.payload_json.get("execution_id") == rows[0].pk
    assert "DreamAgent did not copy" in note.body


@pytest.mark.django_db
def test_copy_now_uses_agent_when_running(copy_relationship, source_trade):
    copy_relationship.auto_execute = True
    copy_relationship.save(update_fields=["auto_execute"])

    with (
        patch("services.dream_agent_service.get_running_agent", return_value=object()),
        patch("services.dream_agent_service.evaluate_and_maybe_execute") as mock_exec,
    ):
        mock_exec.return_value = None
        detect_and_process_copy(source_trade)

    mock_exec.assert_called_once()
    assert CopyExecution.objects.filter(source_trade=source_trade).count() == 0


@pytest.mark.django_db
def test_patch_follow_action_toggles_auto_execute(client, user, copy_relationship):
    client.force_login(user)
    assert copy_relationship.auto_execute is False
    res = client.patch(
        f"/api/copy/{copy_relationship.pk}/",
        data={"auto_execute": True},
        content_type="application/json",
    )
    assert res.status_code == 200
    copy_relationship.refresh_from_db()
    assert copy_relationship.auto_execute is True
    res = client.patch(
        f"/api/copy/{copy_relationship.pk}/",
        data={"auto_execute": False},
        content_type="application/json",
    )
    assert res.status_code == 200
    copy_relationship.refresh_from_db()
    assert copy_relationship.auto_execute is False
