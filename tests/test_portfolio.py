"""Portfolio fill import and page rendering."""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

import pytest
from django.utils import timezone

from apps.agents.models import SmartAccount
from apps.portfolio.models import Position
from apps.trading.models import Trade
from integrations.dreamdex.types import FillDTO
from services.portfolio_service import sync_trades_from_fills


def _seed_user_fill(
    mock_adapter,
    event,
    *,
    taker: str,
    fill_id: str = "fill-user-1",
    side: str = "YES",
    quantity: Decimal = Decimal("20"),
    price: Decimal = Decimal("0.72"),
    tx_hash: str = "0x" + "ab" * 32,
    minutes_ago: int = 15,
) -> FillDTO:
    pool = event.pool_address
    fill = FillDTO(
        id=fill_id,
        market_id=event.external_id,
        pool=pool,
        fill_price=price,
        quantity=quantity,
        quote_quantity=price * quantity,
        maker="0xAlpha000000000000000000000000000000000001",
        taker=taker,
        maker_side="NO" if side == "YES" else "YES",
        taker_side=side,
        kind="MARKET",
        taker_is_bid=side == "YES",
        taker_order="order-user-1",
        timestamp=timezone.now() - timedelta(minutes=minutes_ago),
        tx_hash=tx_hash,
        trader_label=None,
    )
    mock_adapter._fills.setdefault(pool, []).append(fill)
    if pool.lower() != pool:
        mock_adapter._fills.setdefault(pool.lower(), []).append(fill)
    return fill


@pytest.mark.django_db
def test_sync_trades_from_fills_creates_position(user, wallet, sample_event, mock_adapter):
    _seed_user_fill(mock_adapter, sample_event, taker=wallet.address)

    stats = sync_trades_from_fills(user, force=True)
    assert stats["imported"] == 1

    trade = Trade.objects.get(user=user)
    assert trade.status == Trade.Status.CONFIRMED
    assert trade.amount == Decimal("20")
    assert trade.entry_price == Decimal("0.72")
    assert trade.outcome.outcome_type == "YES"
    assert trade.transaction_hash.startswith("0x")
    assert timezone.now() - trade.opened_at < timedelta(minutes=20)

    from services.portfolio_service import sync_positions

    sync_positions(user)
    position = Position.objects.get(user=user, event=sample_event)
    assert position.status == Position.Status.OPEN
    assert position.amount == Decimal("20")


@pytest.mark.django_db
def test_sync_trades_from_fills_is_idempotent(user, wallet, sample_event, mock_adapter):
    _seed_user_fill(mock_adapter, sample_event, taker=wallet.address)
    sync_trades_from_fills(user, force=True)
    stats = sync_trades_from_fills(user, force=True)
    assert stats["imported"] == 0
    assert Trade.objects.filter(user=user).count() == 1


@pytest.mark.django_db
def test_sync_skips_ui_confirmed_tx(user, wallet, sample_event, mock_adapter):
    fill = _seed_user_fill(mock_adapter, sample_event, taker=wallet.address)
    yes = sample_event.outcomes.get(outcome_type="YES")
    Trade.objects.create(
        user=user,
        event=sample_event,
        outcome=yes,
        side=Trade.Side.BUY,
        amount=Decimal("20"),
        entry_price=Decimal("0.72"),
        transaction_hash=fill.tx_hash,
        status=Trade.Status.CONFIRMED,
        external_trade_id="",
    )
    stats = sync_trades_from_fills(user, force=True)
    assert stats["imported"] == 0
    assert Trade.objects.filter(user=user).count() == 1


@pytest.mark.django_db
def test_sync_imports_smart_account_fills(user, wallet, sample_event, mock_adapter, settings):
    sa = SmartAccount.objects.create(
        user=user,
        owner_address=wallet.address,
        address="0x99dabf51dD6a3A0D2b839AB48403436EDf615cAf",
        chain_id=settings.DREAMDEX_CHAIN_ID,
        status=SmartAccount.Status.DEPLOYED,
    )
    _seed_user_fill(
        mock_adapter,
        sample_event,
        taker=sa.address,
        fill_id="fill-sa-1",
        tx_hash="0x" + "cd" * 32,
    )
    stats = sync_trades_from_fills(user, force=True)
    assert stats["imported"] == 1
    trade = Trade.objects.get(user=user)
    assert trade.metadata_json["wallet"] == sa.address.lower()


@pytest.mark.django_db
def test_portfolio_page_shows_onchain_fill(client, user, wallet, sample_event, mock_adapter):
    _seed_user_fill(mock_adapter, sample_event, taker=wallet.address)
    client.force_login(user)
    res = client.get("/portfolio/")
    assert res.status_code == 200
    from services.event_copy import event_question

    body = res.content.decode()
    assert event_question(sample_event) in body
    assert "Open positions" in body
    assert "Trades" in body
    assert "20.00" in body
    assert Position.objects.filter(user=user).exists()


@pytest.mark.django_db
def test_portfolio_empty_when_wallet_has_no_fills(client, user, wallet):
    client.force_login(user)
    res = client.get("/portfolio/")
    assert res.status_code == 200
    assert b"No positions yet" in res.content
    assert b"https://t.me/userinfobot" in res.content
    assert b"How to get your chat ID" in res.content
    assert b"Open the DreamLens bot" in res.content or b"Open DreamLens bot" in res.content
    assert b"Not linked" in res.content
    assert Trade.objects.filter(user=user).count() == 0
    assert Position.objects.filter(user=user).count() == 0
    assert b"Following" in res.content
    assert b"Find traders" in res.content


@pytest.mark.django_db
def test_portfolio_lists_followed_traders_and_action(client, user, copy_relationship):
    client.force_login(user)
    res = client.get("/portfolio/")
    assert res.status_code == 200
    body = res.content.decode()
    assert 'data-authenticated="1"' in body
    assert "Following" in body
    assert "AlphaTrader" in body
    assert "Copy now" in body
    assert "Notify me" in body
    assert "Alerts on Telegram and DreamLens" in body
    assert f'data-copy-action="{copy_relationship.pk}"' in body


@pytest.mark.django_db
def test_refresh_portfolio_settles_winner(user, wallet, sample_event, mock_adapter):
    _seed_user_fill(mock_adapter, sample_event, taker=wallet.address)
    mock_adapter.simulate_settlement(sample_event.external_id, "YES")

    from apps.events.models import EventContract
    from services.portfolio_service import refresh_portfolio

    refresh_portfolio(user)
    sample_event.refresh_from_db()
    assert sample_event.status == EventContract.Status.RESOLVED
    assert sample_event.winning_outcome == "YES"

    position = Position.objects.get(user=user, event=sample_event)
    assert position.status == Position.Status.SETTLED
    assert position.pnl == Decimal("5.6000")
    from services.portfolio_service import position_result

    assert position_result(position) == "won"


@pytest.mark.django_db
def test_refresh_portfolio_settles_loser(user, wallet, sample_event, mock_adapter):
    _seed_user_fill(mock_adapter, sample_event, taker=wallet.address)
    mock_adapter.simulate_settlement(sample_event.external_id, "NO")

    from services.portfolio_service import position_result, refresh_portfolio

    refresh_portfolio(user)
    position = Position.objects.get(user=user)
    assert position.status == Position.Status.SETTLED
    assert position.pnl == Decimal("-14.4000")
    assert position_result(position) == "lost"


@pytest.mark.django_db
def test_voided_market_pays_half(user, wallet, sample_event, mock_adapter):
    _seed_user_fill(mock_adapter, sample_event, taker=wallet.address)
    mock_adapter.simulate_void(sample_event.external_id)

    from apps.events.models import EventContract
    from services.portfolio_service import position_result, refresh_portfolio

    refresh_portfolio(user)
    sample_event.refresh_from_db()
    assert sample_event.status == EventContract.Status.VOIDED
    position = Position.objects.get(user=user)
    assert position.status == Position.Status.SETTLED
    assert position.pnl == Decimal("-4.4000")
    assert position_result(position) == "void"


@pytest.mark.django_db
def test_expired_unresolved_shows_settling(client, user, wallet, sample_event, mock_adapter):
    _seed_user_fill(mock_adapter, sample_event, taker=wallet.address)
    mock_adapter.simulate_lock(sample_event.external_id)
    client.force_login(user)
    res = client.get("/portfolio/")
    body = res.content.decode()
    assert res.status_code == 200
    assert "Settling" in body
    assert "waiting for oracle" in body
    assert Position.objects.get(user=user).status == Position.Status.OPEN


@pytest.mark.django_db
def test_portfolio_page_shows_won_and_claim(client, user, wallet, sample_event, mock_adapter):
    _seed_user_fill(mock_adapter, sample_event, taker=wallet.address)
    mock_adapter.simulate_settlement(sample_event.external_id, "YES")
    client.force_login(user)
    res = client.get("/portfolio/")
    body = res.content.decode()
    assert res.status_code == 200
    assert "Won" in body
    assert "Claim" in body
    assert "Closed" in body
    position = Position.objects.get(user=user)
    assert position.status == Position.Status.SETTLED


@pytest.mark.django_db
def test_redeem_api_claims_winnings(client, user, wallet, sample_event, mock_adapter):
    _seed_user_fill(mock_adapter, sample_event, taker=wallet.address)
    mock_adapter.simulate_settlement(sample_event.external_id, "YES")
    from services.portfolio_service import refresh_portfolio

    refresh_portfolio(user)
    position = Position.objects.get(user=user)
    client.force_login(user)
    prepared = client.post(
        f"/api/portfolio/positions/{position.pk}/redeem/",
        data={"wallet_address": wallet.address},
        content_type="application/json",
    )
    assert prepared.status_code == 200
    payload = prepared.json()
    assert payload["unsigned_tx"]["to"]
    assert payload["unsigned_tx"]["data"].startswith("0x")
    assert payload["outcome_idx"] == 0

    confirmed = client.post(
        f"/api/portfolio/positions/{position.pk}/redeem/confirm/",
        data={"tx_hash": "0x" + "ab" * 32},
        content_type="application/json",
    )
    assert confirmed.status_code == 200
    position.refresh_from_db()
    assert position.status == Position.Status.CLOSED
    page = client.get("/portfolio/")
    assert b"Claimed" in page.content
    assert b"Claim $" not in page.content


@pytest.mark.django_db
def test_portfolio_page_shows_close_on_open_position(
    client, user, wallet, sample_event, mock_adapter
):
    _seed_user_fill(mock_adapter, sample_event, taker=wallet.address)
    client.force_login(user)
    res = client.get("/portfolio/")
    assert res.status_code == 200
    body = res.content.decode()
    assert "data-close-position" in body
    assert "Close" in body
    event_page = client.get(f"/events/{sample_event.pk}/")
    assert event_page.status_code == 200
    assert b"data-close-position" in event_page.content
    assert b"Close trade" in event_page.content


@pytest.mark.django_db
def test_close_api_sells_open_position(client, user, wallet, sample_event, mock_adapter):
    _seed_user_fill(mock_adapter, sample_event, taker=wallet.address)
    from services.portfolio_service import refresh_portfolio

    refresh_portfolio(user)
    position = Position.objects.get(user=user)
    assert position.status == Position.Status.OPEN
    client.force_login(user)
    prepared = client.post(
        f"/api/portfolio/positions/{position.pk}/close/",
        data={"wallet_address": wallet.address},
        content_type="application/json",
    )
    assert prepared.status_code == 200
    payload = prepared.json()
    assert payload["unsigned_tx"]["to"]
    assert payload["unsigned_tx"]["data"].startswith("0x")
    assert payload["trade_id"]
    assert payload["outcome"] == "YES"

    confirmed = client.post(
        f"/api/portfolio/positions/{position.pk}/close/confirm/",
        data={"tx_hash": "0x" + "cd" * 32, "trade_id": payload["trade_id"]},
        content_type="application/json",
    )
    assert confirmed.status_code == 200
    position.refresh_from_db()
    assert position.status == Position.Status.CLOSED
    sell = Trade.objects.get(pk=payload["trade_id"])
    assert sell.side == Trade.Side.SELL
    assert sell.status == Trade.Status.CONFIRMED
    page = client.get("/portfolio/")
    body = page.content.decode()
    assert 'data-close-position="' not in body
    assert "Closed" in body


@pytest.mark.django_db
def test_close_api_rejects_settled_position(
    client, user, wallet, sample_event, mock_adapter
):
    _seed_user_fill(mock_adapter, sample_event, taker=wallet.address)
    mock_adapter.simulate_settlement(sample_event.external_id, "YES")
    from services.portfolio_service import refresh_portfolio

    refresh_portfolio(user)
    position = Position.objects.get(user=user)
    client.force_login(user)
    prepared = client.post(
        f"/api/portfolio/positions/{position.pk}/close/",
        data={"wallet_address": wallet.address},
        content_type="application/json",
    )
    assert prepared.status_code == 400
    assert "claim" in prepared.json()["detail"].lower() or "settlement" in prepared.json()["detail"].lower()


@pytest.mark.django_db
def test_sync_positions_nets_sells(user, sample_event):
    yes = sample_event.outcomes.get(outcome_type="YES")
    Trade.objects.create(
        user=user,
        event=sample_event,
        outcome=yes,
        side=Trade.Side.BUY,
        amount=Decimal("20"),
        entry_price=Decimal("0.72"),
        status=Trade.Status.CONFIRMED,
    )
    Trade.objects.create(
        user=user,
        event=sample_event,
        outcome=yes,
        side=Trade.Side.SELL,
        amount=Decimal("20"),
        entry_price=Decimal("0.80"),
        status=Trade.Status.CONFIRMED,
    )
    from services.portfolio_service import position_result, sync_positions

    sync_positions(user)
    position = Position.objects.get(user=user)
    assert position.status == Position.Status.CLOSED
    assert position.pnl == Decimal("1.6000")
    assert position_result(position) == "closed"


@pytest.mark.django_db
def test_redeem_api_returns_json_when_gas_estimate_reverts(
    client, user, wallet, sample_event, mock_adapter
):
    _seed_user_fill(mock_adapter, sample_event, taker=wallet.address)
    mock_adapter.simulate_settlement(sample_event.external_id, "YES")
    from services.portfolio_service import refresh_portfolio

    def boom(*_args, **_kwargs):
        raise RuntimeError("execution reverted")

    mock_adapter.quote_wallet_fees = boom
    refresh_portfolio(user)
    position = Position.objects.get(user=user)
    client.force_login(user)
    prepared = client.post(
        f"/api/portfolio/positions/{position.pk}/redeem/",
        data={"wallet_address": wallet.address},
        content_type="application/json",
    )
    assert prepared.status_code == 200
    assert prepared.headers["Content-Type"].startswith("application/json")
    assert prepared.json()["unsigned_tx"]["data"].startswith("0x")


@pytest.mark.django_db
def test_redeem_api_rejects_smart_account_claim_without_grant(
    client, user, wallet, sample_event, mock_adapter, settings
):
    sa_addr = "0x481B210d927765133d55461c3EaCC96F41FdD6C3"
    SmartAccount.objects.create(
        user=user,
        owner_address=wallet.address,
        address=sa_addr,
        chain_id=settings.DREAMDEX_CHAIN_ID,
        status=SmartAccount.Status.DEPLOYED,
    )
    _seed_user_fill(mock_adapter, sample_event, taker=sa_addr)
    mock_adapter.simulate_settlement(sample_event.external_id, "YES")
    from services.portfolio_service import refresh_portfolio

    refresh_portfolio(user)
    position = Position.objects.get(user=user)
    client.force_login(user)
    prepared = client.post(
        f"/api/portfolio/positions/{position.pk}/redeem/",
        data={"wallet_address": wallet.address},
        content_type="application/json",
    )
    assert prepared.status_code == 400
    assert "smart account" in prepared.json()["detail"].lower()


@pytest.mark.django_db
def test_redeem_api_session_claims_smart_account_tokens(
    client, user, wallet, sample_event, mock_adapter, settings
):
    settings.MOCK_SMART_ACCOUNT = True
    from services import smart_account_service
    from services.portfolio_service import refresh_portfolio

    sa = smart_account_service.create_account(user, owner_address=wallet.address)
    smart_account_service.mark_funded(sa, amount=Decimal("50"))
    smart_account_service.grant_agent(
        user,
        max_trade_amount=Decimal("10"),
        max_daily_volume=Decimal("50"),
        expires_in_days=30,
        min_copy_score=50,
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
    _seed_user_fill(mock_adapter, sample_event, taker=sa.address)
    mock_adapter.simulate_settlement(sample_event.external_id, "YES")
    refresh_portfolio(user)
    position = Position.objects.get(user=user)
    client.force_login(user)
    prepared = client.post(
        f"/api/portfolio/positions/{position.pk}/redeem/",
        data={"wallet_address": wallet.address},
        content_type="application/json",
    )
    assert prepared.status_code == 200
    payload = prepared.json()
    assert payload["claimed"] is True
    assert payload["tx_hash"].startswith("0x")
    position.refresh_from_db()
    assert position.status == Position.Status.CLOSED


@pytest.mark.django_db
def test_redeem_api_dreamagent_claim_needs_no_wallet(
    client, user, wallet, sample_event, mock_adapter, settings
):
    settings.MOCK_SMART_ACCOUNT = True
    from services import smart_account_service
    from services.portfolio_service import refresh_portfolio

    sa = smart_account_service.create_account(user, owner_address=wallet.address)
    smart_account_service.mark_funded(sa, amount=Decimal("50"))
    smart_account_service.grant_agent(
        user,
        max_trade_amount=Decimal("10"),
        max_daily_volume=Decimal("50"),
        expires_in_days=30,
        min_copy_score=50,
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
    _seed_user_fill(mock_adapter, sample_event, taker=sa.address)
    mock_adapter.simulate_settlement(sample_event.external_id, "YES")
    refresh_portfolio(user)
    position = Position.objects.get(user=user)
    client.force_login(user)
    page = client.get("/portfolio/")
    assert b"data-claim-agent" in page.content
    assert b"DreamAgent claim" in page.content
    assert b"DreamAgent claims" in page.content
    assert b"no MetaMask" in page.content
    agent_page = client.get("/agent/")
    assert agent_page.status_code == 200
    assert b"data-claim-agent" in agent_page.content
    prepared = client.post(
        f"/api/portfolio/positions/{position.pk}/redeem/",
        data={},
        content_type="application/json",
    )
    assert prepared.status_code == 200
    assert prepared.json()["claimed"] is True
    position.refresh_from_db()
    assert position.status == Position.Status.CLOSED


@pytest.mark.django_db
def test_agent_claim_all_redeems_smart_account_wins(
    client, user, wallet, sample_event, mock_adapter, settings
):
    settings.MOCK_SMART_ACCOUNT = True
    from services import smart_account_service
    from services.portfolio_service import refresh_portfolio

    sa = smart_account_service.create_account(user, owner_address=wallet.address)
    smart_account_service.mark_funded(sa, amount=Decimal("50"))
    smart_account_service.grant_agent(
        user,
        max_trade_amount=Decimal("10"),
        max_daily_volume=Decimal("50"),
        expires_in_days=30,
        min_copy_score=50,
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
    _seed_user_fill(mock_adapter, sample_event, taker=sa.address)
    mock_adapter.simulate_settlement(sample_event.external_id, "YES")
    refresh_portfolio(user)
    client.force_login(user)
    res = client.post("/api/portfolio/claim/", data={}, content_type="application/json")
    assert res.status_code == 200
    payload = res.json()
    assert payload["claimed"] == 1
    assert payload["via_smart_account"] is True


@pytest.mark.django_db
def test_agent_claim_refuses_grant_without_redeem(
    client, user, wallet, sample_event, mock_adapter, settings
):
    from eth_utils import function_signature_to_4byte_selector
    from integrations.metamask import PLACE_BINARY_ORDER_SELECTOR
    from services import smart_account_service
    from services.portfolio_service import refresh_portfolio

    settings.MOCK_SMART_ACCOUNT = True
    sa = smart_account_service.create_account(user, owner_address=wallet.address)
    smart_account_service.mark_funded(sa, amount=Decimal("50"))
    packed = (
        function_signature_to_4byte_selector(PLACE_BINARY_ORDER_SELECTOR)
        + function_signature_to_4byte_selector("approve(address,uint256)")
    )
    smart_account_service.grant_agent(
        user,
        max_trade_amount=Decimal("10"),
        max_daily_volume=Decimal("50"),
        expires_in_days=30,
        min_copy_score=50,
        signed_delegation={
            "delegate": "0xSession00000000000000000000000000000001",
            "delegator": sa.address,
            "authority": "0xffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff",
            "caveats": [
                {
                    "enforcer": settings.METAMASK_ALLOWED_METHODS_ENFORCER,
                    "terms": "0x" + packed.hex(),
                    "args": "0x",
                }
            ],
            "salt": "0x1",
            "signature": "0xmockdeadbeef",
            "mock": True,
        },
        activate=True,
    )
    _seed_user_fill(mock_adapter, sample_event, taker=sa.address)
    mock_adapter.simulate_settlement(sample_event.external_id, "YES")
    refresh_portfolio(user)
    client.force_login(user)
    res = client.post("/api/portfolio/claim/", data={}, content_type="application/json")
    assert res.status_code == 400
    detail = res.json()["detail"].lower()
    assert "redeem" in detail
    assert "activate" in detail
    page = client.get("/portfolio/")
    assert b"Re-sign grant" in page.content
    assert b"cannot claim on Shannon" in page.content


@pytest.mark.django_db
def test_event_detail_shows_claim_on_won_position(
    client, user, wallet, sample_event, mock_adapter
):
    _seed_user_fill(mock_adapter, sample_event, taker=wallet.address)
    mock_adapter.simulate_settlement(sample_event.external_id, "YES")
    from services.portfolio_service import refresh_portfolio

    refresh_portfolio(user)
    client.force_login(user)
    res = client.get(f"/events/{sample_event.pk}/")
    assert res.status_code == 200
    assert b"data-claim-position" in res.content
    assert b"Claim" in res.content
    assert b"Buy YES" not in res.content


@pytest.mark.django_db
def test_portfolio_does_not_auto_claim_smart_account_wins(
    client, user, wallet, sample_event, mock_adapter, settings
):
    settings.MOCK_SMART_ACCOUNT = True
    from services import smart_account_service
    from services.portfolio_service import refresh_portfolio

    sa = smart_account_service.create_account(user, owner_address=wallet.address)
    smart_account_service.mark_funded(sa, amount=Decimal("50"))
    smart_account_service.grant_agent(
        user,
        max_trade_amount=Decimal("10"),
        max_daily_volume=Decimal("50"),
        expires_in_days=30,
        min_copy_score=50,
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
    _seed_user_fill(mock_adapter, sample_event, taker=sa.address)
    mock_adapter.simulate_settlement(sample_event.external_id, "YES")
    refresh_portfolio(user)
    position = Position.objects.get(user=user)
    assert position.status != Position.Status.CLOSED
    client.force_login(user)
    res = client.get("/portfolio/")
    body = res.content.decode()
    assert "DreamAgent claims" in body
    assert f'data-claim-position="{position.pk}"' in body
    assert "data-claim-agent" in body
    closed_start = body.find(">Closed<")
    claim_box = body.find("DreamAgent claims")
    pos_marker = f'data-claim-position="{position.pk}"'
    assert claim_box != -1
    assert pos_marker in body[claim_box:closed_start] if closed_start > claim_box else pos_marker in body
    res = client.post(
        "/api/portfolio/claim/",
        data={"position_id": position.pk},
        content_type="application/json",
    )
    assert res.status_code == 200
    assert res.json()["claimed"] == 1
    position.refresh_from_db()
    assert position.status == Position.Status.CLOSED


@pytest.mark.django_db
def test_portfolio_page_after_smart_account_web_trade(
    client, user, wallet, sample_event, settings
):
    settings.MOCK_SMART_ACCOUNT = True
    from services import smart_account_service
    from services.onboarding_service import first_session_state

    sa = smart_account_service.create_account(user, owner_address=wallet.address)
    smart_account_service.mark_funded(sa, amount=Decimal("50"))
    smart_account_service.grant_agent(
        user,
        max_trade_amount=Decimal("10"),
        max_daily_volume=Decimal("50"),
        expires_in_days=30,
        min_copy_score=50,
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
    yes = sample_event.outcomes.get(outcome_type="YES")
    Trade.objects.create(
        user=user,
        event=sample_event,
        outcome=yes,
        side=Trade.Side.BUY,
        amount=Decimal("1"),
        entry_price=Decimal("0.41"),
        transaction_hash="0x" + "ef" * 32,
        status=Trade.Status.CONFIRMED,
        metadata_json={"smart_account": sa.address, "source": "web", "wallet": sa.address},
    )
    state = first_session_state(user)
    assert state["has_first_trade"] is True
    assert state["incomplete"] is False
    client.force_login(user)
    res = client.get("/portfolio/")
    assert res.status_code == 200
    body = res.content.decode()
    assert "Available" in body
    assert "Hybrid" not in body
    assert "EIP-712" not in body
