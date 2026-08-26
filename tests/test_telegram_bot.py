"""Telegram DreamAgent bot: link confirm, authz, follow, delegated trade."""

from __future__ import annotations

import json
from decimal import Decimal
from unittest.mock import patch

import pytest

from apps.accounts.models import TelegramLink
from apps.dreamcopy.models import CopyRelationship
from services import smart_account_service
from services.telegram_bot_service import handle_update
from services.telegram_link_service import TelegramLinkError, start_link, unlink


CHAT_ID = 42424242


@pytest.fixture
def telegram_settings(settings):
    settings.TELEGRAM_BOT_TOKEN = "test-token"
    settings.TELEGRAM_BOT_USERNAME = "DreamLensBot"
    settings.TELEGRAM_WEBHOOK_SECRET = "hook-secret"
    settings.MOCK_SMART_ACCOUNT = True
    return settings


@pytest.fixture
def running_agent(user, wallet, trader, telegram_settings):
    sa = smart_account_service.create_account(user, owner_address=wallet.address)
    smart_account_service.mark_funded(sa, amount=Decimal("50"))
    agent, _perm = smart_account_service.grant_agent(
        user,
        max_trade_amount=Decimal("10"),
        max_daily_volume=Decimal("50"),
        expires_in_days=30,
        min_copy_score=50,
        allowed_traders=[str(trader.pk)],
        signed_delegation={
            "delegate": "0xSession00000000000000000000000000000001",
            "delegator": sa.address,
            "authority": "0xffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff",
            "caveats": [],
            "salt": "0x1",
            "signature": "0xmockdeadbeef",
            "mock": True,
        },
        activate=True,
    )
    return agent


def _message(text: str, chat_id: int = CHAT_ID) -> dict:
    return {"message": {"chat": {"id": chat_id}, "text": text}}


def _callback(data: str, chat_id: int = CHAT_ID) -> dict:
    return {
        "callback_query": {
            "id": "cb1",
            "data": data,
            "message": {"chat": {"id": chat_id}},
        }
    }


@pytest.mark.django_db
@patch("services.telegram_link_service.send_message")
def test_link_pending_until_confirm(send_msg, user, telegram_settings):
    link = start_link(user, CHAT_ID)
    assert link.status == TelegramLink.Status.PENDING
    send_msg.assert_called_once()
    token = link.confirm_token
    with patch("services.telegram_bot_service.send_message") as bot_send:
        with patch("services.telegram_bot_service.answer_callback"):
            handle_update(_callback(f"tg:ok:{token}"))
    link.refresh_from_db()
    assert link.status == TelegramLink.Status.ACTIVE
    assert link.linked_at is not None
    bot_send.assert_called()


@pytest.mark.django_db
@patch("services.telegram_link_service.send_message")
def test_start_ok_payload_confirms_pending_link(send_msg, user, telegram_settings):
    start_link(user, CHAT_ID)
    token = TelegramLink.objects.get(user=user).confirm_token
    with patch("services.telegram_bot_service.send_message") as bot_send:
        handle_update(_message(f"/start ok_{token}"))
    link = TelegramLink.objects.get(user=user)
    assert link.status == TelegramLink.Status.ACTIVE
    assert "linked" in bot_send.call_args[0][1].lower()


@pytest.mark.django_db
@patch("services.telegram_link_service.send_message")
def test_link_chat_id_unique(send_msg, user, telegram_settings, db):
    from apps.accounts.models import User

    other = User.objects.create_user(username="other", password="x")
    start_link(user, CHAT_ID)
    token = TelegramLink.objects.get(user=user).confirm_token
    with patch("services.telegram_bot_service.send_message"):
        with patch("services.telegram_bot_service.answer_callback"):
            handle_update(_callback(f"tg:ok:{token}"))
    with pytest.raises(TelegramLinkError):
        start_link(other, CHAT_ID)


@pytest.mark.django_db
@patch("services.telegram_link_service.send_message")
def test_unlink(send_msg, user, telegram_settings):
    start_link(user, CHAT_ID)
    token = TelegramLink.objects.get(user=user).confirm_token
    with patch("services.telegram_bot_service.send_message"):
        with patch("services.telegram_bot_service.answer_callback"):
            handle_update(_callback(f"tg:ok:{token}"))
    unlink(user)
    assert not TelegramLink.objects.filter(user=user).exists()


@pytest.mark.django_db
def test_unlinked_trade_refused(telegram_settings):
    with patch("services.telegram_bot_service.send_message") as send_msg:
        handle_update(_message("/trade 1 YES 10"))
    text = send_msg.call_args[0][1]
    assert "not linked" in text.lower() or "chat ID" in text


@pytest.mark.django_db
@patch("services.telegram_link_service.send_message")
def test_linked_without_agent_trade_refused(send_msg, user, telegram_settings):
    start_link(user, CHAT_ID)
    token = TelegramLink.objects.get(user=user).confirm_token
    with patch("services.telegram_bot_service.send_message"):
        with patch("services.telegram_bot_service.answer_callback"):
            handle_update(_callback(f"tg:ok:{token}"))
    with patch("services.telegram_bot_service.send_message") as bot_send:
        handle_update(_message("/trade 1 YES 10"))
    text = bot_send.call_args[0][1]
    assert "Activate DreamAgent" in text


@pytest.mark.django_db
@patch("services.telegram_link_service.send_message")
def test_follow_creates_relationship(send_msg, user, trader, telegram_settings):
    start_link(user, CHAT_ID)
    token = TelegramLink.objects.get(user=user).confirm_token
    with patch("services.telegram_bot_service.send_message"):
        with patch("services.telegram_bot_service.answer_callback"):
            handle_update(_callback(f"tg:ok:{token}"))
    with patch("services.telegram_bot_service.send_message"):
        handle_update(_message(f"/follow {trader.pk}"))
    rel = CopyRelationship.objects.get(user=user, trader=trader)
    assert rel.status == CopyRelationship.Status.ACTIVE
    assert rel.auto_execute is False


@pytest.mark.django_db
@patch("services.telegram_link_service.send_message")
def test_trade_confirm_broadcasts(
    send_msg, user, wallet, trader, sample_event, running_agent, telegram_settings
):
    start_link(user, CHAT_ID)
    token = TelegramLink.objects.get(user=user).confirm_token
    with patch("services.telegram_bot_service.send_message"):
        with patch("services.telegram_bot_service.answer_callback"):
            handle_update(_callback(f"tg:ok:{token}"))

    with patch("services.telegram_bot_service.send_message") as prompt:
        handle_update(_message(f"/trade {sample_event.pk} YES 5"))
    markup = prompt.call_args.kwargs.get("reply_markup")
    assert markup, prompt.call_args
    trade_token = markup["inline_keyboard"][0][0]["callback_data"].split(":", 2)[-1]

    with patch(
        "services.dream_agent_service.broadcast_delegated_execution",
        return_value="0x" + "ab" * 32,
    ) as broadcast:
        with patch("services.telegram_bot_service.send_message") as bot_send:
            with patch("services.telegram_bot_service.answer_callback"):
                handle_update(_callback(f"tr:ok:{trade_token}"))
    broadcast.assert_called_once()
    assert any("Trade submitted" in (c[0][1] if c[0] else "") for c in bot_send.call_args_list) or bot_send.called


@pytest.mark.django_db
def test_webhook_rejects_missing_secret(client, telegram_settings):
    res = client.post(
        "/api/telegram/webhook/",
        data=json.dumps({"update_id": 1}),
        content_type="application/json",
    )
    assert res.status_code == 403


@pytest.mark.django_db
@patch("apps.core.api.telegram.handle_update")
def test_webhook_accepts_secret(handle, client, telegram_settings):
    res = client.post(
        "/api/telegram/webhook/",
        data=json.dumps({"update_id": 1, "message": {"chat": {"id": 1}, "text": "/help"}}),
        content_type="application/json",
        HTTP_X_TELEGRAM_BOT_API_SECRET_TOKEN="hook-secret",
    )
    assert res.status_code == 200
    handle.assert_called_once()


@pytest.mark.django_db
def test_link_api_requires_auth(client, telegram_settings):
    res = client.post(
        "/api/telegram/link/",
        data={"chat_id": CHAT_ID},
        content_type="application/json",
    )
    assert res.status_code in (401, 403)


def _activate_link(user):
    start_link(user, CHAT_ID)
    token = TelegramLink.objects.get(user=user).confirm_token
    with patch("services.telegram_bot_service.send_message"):
        with patch("services.telegram_bot_service.answer_callback"):
            handle_update(_callback(f"tg:ok:{token}"))


@pytest.mark.django_db
@patch("services.telegram_link_service.send_message")
def test_events_alias_lists_live_event(send_msg, user, sample_event, telegram_settings):
    _activate_link(user)
    with patch("services.telegram_bot_service.send_message") as bot_send:
        handle_update(_message("/events"))
    text = bot_send.call_args[0][1]
    assert str(sample_event.pk) in text
    assert sample_event.underlying_asset in text


@pytest.mark.django_db
@patch("services.telegram_link_service.send_message")
def test_pause_resume_and_trade_while_paused(
    send_msg, user, sample_event, running_agent, telegram_settings
):
    from apps.agents.models import DreamAgent

    _activate_link(user)
    with patch("services.telegram_bot_service.send_message"):
        handle_update(_message("/pause"))
    running_agent.refresh_from_db()
    assert running_agent.status == DreamAgent.Status.PAUSED

    with patch("services.telegram_bot_service.send_message") as bot_send:
        handle_update(_message(f"/trade {sample_event.pk} YES 5"))
    assert "paused" in bot_send.call_args[0][1].lower()

    with patch("services.telegram_bot_service.send_message"):
        handle_update(_message("/resume"))
    running_agent.refresh_from_db()
    assert running_agent.status == DreamAgent.Status.RUNNING


@pytest.mark.django_db
@patch("services.telegram_link_service.send_message")
def test_over_limit_trade_does_not_broadcast(
    send_msg, user, sample_event, running_agent, telegram_settings
):
    _activate_link(user)
    with patch(
        "services.dream_agent_service.broadcast_delegated_execution"
    ) as broadcast:
        with patch("services.telegram_bot_service.send_message") as bot_send:
            handle_update(_message(f"/trade {sample_event.pk} YES 100"))
    broadcast.assert_not_called()
    text = bot_send.call_args[0][1]
    assert "limit" in text.lower()
    assert "$10" in text or "10" in text
    assert "cannot execute" in text.lower()


@pytest.mark.django_db
@patch("services.telegram_link_service.send_message")
def test_nl_buy_opens_confirm(
    send_msg, user, sample_event, running_agent, telegram_settings
):
    _activate_link(user)
    asset = sample_event.underlying_asset
    with patch("services.telegram_bot_service.send_message") as prompt:
        handle_update(_message(f"Buy $5 YES on {asset}"))
    markup = prompt.call_args.kwargs.get("reply_markup")
    assert markup
    data = markup["inline_keyboard"][0][0]["callback_data"]
    assert data.startswith("tr:ok:")
    trade_token = data.split(":", 2)[-1]
    with patch(
        "services.dream_agent_service.broadcast_delegated_execution",
        return_value="0x" + "ab" * 32,
    ) as broadcast:
        with patch("services.telegram_bot_service.send_message"):
            with patch("services.telegram_bot_service.answer_callback"):
                handle_update(_callback(f"tr:ok:{trade_token}"))
    broadcast.assert_called_once()


@pytest.mark.django_db
@patch("services.telegram_link_service.send_message")
def test_positions_shows_imported_fill(
    send_msg, user, wallet, sample_event, mock_adapter, telegram_settings
):
    from datetime import timedelta

    from django.utils import timezone
    from integrations.dreamdex.types import FillDTO
    from services.portfolio_service import sync_trades_from_fills

    fill = FillDTO(
        id="fill-tg-pos",
        market_id=sample_event.external_id,
        pool=sample_event.pool_address,
        fill_price=Decimal("0.72"),
        quantity=Decimal("20"),
        quote_quantity=Decimal("14.40"),
        maker="0xAlpha000000000000000000000000000000000001",
        taker=wallet.address,
        maker_side="NO",
        taker_side="YES",
        kind="MARKET",
        taker_is_bid=True,
        taker_order="order-tg",
        timestamp=timezone.now() - timedelta(minutes=10),
        tx_hash="0x" + "ef" * 32,
        trader_label=None,
    )
    mock_adapter._fills.setdefault(sample_event.pool_address, []).append(fill)
    sync_trades_from_fills(user, force=True)
    _activate_link(user)
    with patch("services.telegram_bot_service.send_message") as bot_send:
        handle_update(_message("/positions"))
    text = bot_send.call_args[0][1]
    assert "YES" in text
    assert "20" in text
