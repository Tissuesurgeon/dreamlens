"""User-facing frontend: language helpers, IA, receipts, trade copy."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest
from django.urls import reverse

from apps.dreamcopy.models import CopyExecution
from apps.events.models import EventContract
from services.event_copy import (
    CLAIM_VS_CLOSE,
    SCORE_DISCLAIMER,
    as_cents,
    event_question,
    event_window_copy,
    format_event_card_text,
    format_payout_block,
    format_window_line,
    payout_math,
    watching_brief,
)
from services import telegram_bot_service


@pytest.mark.django_db
def test_event_question_and_cents_match_telegram(sample_event):
    sample_event.metadata_json = {"opening_price": "118500"}
    sample_event.save(update_fields=["metadata_json"])
    question = event_question(sample_event)
    assert question.startswith("Will ")
    assert "at expiry?" in question
    assert "$118,500.00" in question
    yes = sample_event.outcomes.get(outcome_type="YES")
    text = format_event_card_text(sample_event)
    assert question in text
    assert f"YES {as_cents(yes.current_price)}" in text
    assert "NO " in text
    assert "Ends in" in text
    assert format_event_card_text(sample_event) == telegram_bot_service.format_event_card_text(sample_event)
    assert "%" not in text or "chance" not in text.lower()


def test_payout_labels():
    block = format_payout_block(Decimal("5"), Decimal("0.41"))
    assert "You pay $5.00" in block
    assert "Maximum possible payout" in block
    assert "Potential profit" in block
    assert "Maximum loss $5.00" in block
    assert "What could I receive" not in block
    math = payout_math(Decimal("5"), Decimal("0.41"))
    assert math["payout"] == Decimal("12.20")
    assert math["profit"] == Decimal("7.20")


@pytest.mark.django_db
def test_explore_redirects_to_discover(client):
    res = client.get("/explore/")
    assert res.status_code == 302
    assert res["Location"].endswith("/discover/")


@pytest.mark.django_db
def test_nav_and_testnet_chrome(client):
    res = client.get("/home/")
    assert res.status_code == 200
    body = res.content.decode()
    assert 'data-authenticated="1"' not in body.split("<body", 1)[-1].split(">", 1)[0]
    assert ">Home<" in body
    assert 'href="/" class="dl-brand"' in body
    assert ">Discover<" in body
    assert "Smart Copy" in body
    assert ">Agent<" in body
    assert ">Portfolio<" in body
    assert "Copy" in body
    assert "Me" in body
    assert "Testnet" in body
    assert "no real monetary value" in body
    assert 'href="/lens/"' in body
    assert ">Lens<" not in body.split("dl-nav", 1)[-1].split("</nav>", 1)[0]
    assert "Continue setup" not in body


@pytest.mark.django_db
def test_home_has_look_at(client, sample_event):
    res = client.get("/home/")
    assert res.status_code == 200
    body = res.content.decode()
    assert event_question(sample_event) in body
    assert "Also watching" in body or "Understand" in body
    assert "Live market feed" in body
    assert as_cents(sample_event.outcomes.get(outcome_type="YES").current_price) in body
    assert ">Home<" in body
    assert "What should I look at?" not in body
    assert "YES means you think this happens" in body


@pytest.mark.django_db
def test_discover_intent_filters_and_score(client, sample_event):
    res = client.get("/discover/")
    assert res.status_code == 200
    body = res.content.decode()
    assert "Moving Fast" in body
    assert "High DreamLens Score" in body
    assert "Ending Soon" in body
    assert "Traders Are Active" in body
    assert event_question(sample_event) in body
    assert as_cents(sample_event.outcomes.get(outcome_type="YES").current_price) in body
    assert "DreamLens Score" in body
    assert SCORE_DISCLAIMER in body
    assert "% chance" not in body.lower()
    assert "Put $1" in body
    assert "Explain this" in body
    assert "See this question" in body
    assert 'id="ai-search-form"' not in body
    assert "Live market feed" in body


@pytest.mark.django_db
def test_event_detail_both_sides_and_explain(client, sample_event):
    res = client.get(reverse("event_detail", args=[sample_event.pk]))
    assert res.status_code == 200
    body = res.content.decode()
    assert "What do you think will happen?" in body
    assert "Your possible result" in body
    assert "Maximum possible payout" in body
    assert "Potential profit" in body
    assert "Maximum loss" in body
    assert "Why DreamLens is watching" in body
    assert "DreamLens Score" in body
    assert "YES is" in body
    assert "Fills" in body
    assert "Traders" in body
    assert "Liquidity" in body
    assert "chance of winning" not in body.lower()
    assert "Explain this market" in body
    assert "News that can move this window" in body
    assert 'id="explain-sheet"' in body
    assert 'id="market-reader-template"' in body
    assert "DreamLens is reading this market" in body
    assert "dl-modal__footer-primary" in body
    assert "dl-modal__footer-secondary" in body
    assert "DreamLens explains" in body
    assert "Lens" in body[body.find("explain-sheet") : body.find("explain-sheet") + 800]
    assert "This is a market price, not a guarantee." in body
    assert "Got it" in body
    assert "Buy YES" in body
    explain = body[body.find("explain-sheet") : body.find("explain-sheet") + 2000]
    assert "Buy YES" not in explain
    assert "Review trade" in body
    assert "Trade Check" in body
    assert "I understand that I can lose the amount I paid." in body
    assert "when this event expires" not in body
    assert "Ends in Expired" not in body


@pytest.mark.django_db
def test_window_copy_never_says_ends_in_expired(sample_event, expired_event):
    live = event_window_copy(sample_event)
    assert live["open"] is True
    assert live["line"].startswith("Ends in ")
    assert "Expired" not in live["line"]
    assert format_window_line(sample_event) == live["line"]

    closed = event_window_copy(expired_event)
    assert closed["open"] is False
    assert closed["line"] == "Trading ended · settling"
    assert closed["kicker"] == "This window has closed"
    assert "oracle" in closed["blurb"]
    assert "Ends in" not in closed["line"]
    card = format_event_card_text(expired_event)
    assert "Ends in Expired" not in card
    assert "Trading ended · settling" in card

    expired_event.status = EventContract.Status.RESOLVED
    expired_event.winning_outcome = "YES"
    settled = event_window_copy(expired_event)
    assert settled["line"] == "Settled · YES won"
    assert "claim below or on Portfolio" in settled["blurb"]
    assert "DreamAgent box" in settled["blurb"]


@pytest.mark.django_db
def test_watching_brief_has_facts_not_probability(sample_event):
    brief = watching_brief(sample_event)
    assert 0 <= brief["score"] <= 100
    assert brief["lead"]
    assert brief["reading"]
    assert any(item.startswith("YES ") for item in brief["facts"])
    assert any(item.startswith("NO ") for item in brief["facts"])
    labels = {row["label"] for row in brief["pillars"]}
    assert labels == {"Fills", "Liquidity", "Traders", "Time"}
    assert "chance" not in brief["lead"].lower()
    assert "chance" not in brief["reading"].lower()


@pytest.mark.django_db
def test_watching_brief_closed_window(expired_event):
    brief = watching_brief(expired_event)
    assert brief["open"] is False
    assert "closed" in brief["reading"].lower()
    time_pillar = next(row for row in brief["pillars"] if row["key"] == "time")
    assert time_pillar["band"] == "Closed"


@pytest.mark.django_db
def test_event_detail_expired_writeup(client, expired_event):
    res = client.get(reverse("event_detail", args=[expired_event.pk]))
    assert res.status_code == 200
    body = res.content.decode()
    header = body[body.find("dl-event-header") : body.find("dl-payout-tiles")]
    assert "This window has closed" in header
    assert "Trading ended · settling" in header
    assert "waiting on the oracle" in header
    assert "when this event expires" not in body
    assert "Ends in Expired" not in body
    assert "What do you think will happen?" not in header
    main = body.split('id="trade-modal"', 1)[0]
    assert "Your possible result" not in main
    assert "Buy YES" not in main
    assert "Last price" in body
    assert "Why DreamLens watched this" in body


@pytest.mark.django_db
def test_agent_can_cannot(client):
    res = client.get("/agent/")
    assert res.status_code == 200
    body = res.content.decode()
    assert "Your Dream Agent" in body
    assert "Trade Event Contracts" in body
    assert "Claim Smart Account winnings" in body
    assert "Withdraw your funds" in body
    assert "Change your permissions" in body
    assert "Exceed your limits" in body
    assert "Withdrawal: Never" in body
    assert "What happens next?" in body
    assert "Agent Check" in body


@pytest.mark.django_db
def test_smart_copy_follow_sheet_and_sample_size(client, sample_event):
    from django.core.cache import cache

    from apps.dreamcopy.models import TraderProfile, TraderTrade

    cache.clear()

    trader = TraderProfile.objects.create(
        wallet_address="0x6730d3a2a217108ab53ccfe60ffdad05d3c124e5",
        display_name="AlphaTrader",
        total_trades=31,
        win_rate=Decimal("0.72"),
    )
    yes = sample_event.outcomes.get(outcome_type="YES")
    TraderTrade.objects.create(
        trader=trader,
        event=sample_event,
        outcome=yes,
        entry_price=yes.current_price,
        amount=Decimal("5"),
        opened_at=sample_event.expiry_time,
        external_trade_id="ux-fill-1",
    )
    res = client.get("/following/")
    assert res.status_code == 200
    body = res.content.decode()
    assert "Smart Copy" in body
    assert "See what they trade" in body
    assert "Maximum per trade" in body
    assert "Minimum DreamLens Score" in body
    assert "What happens next?" in body
    assert "Activate Smart Copy" in body
    assert "Copy now" in body
    assert "Notify me" in body
    assert "dl-modal__footer-primary" in body
    assert "Cancel" in body
    assert "Follow a wallet" in body
    assert 'id="follow-wallet-form"' in body
    assert 'id="follow-wallet-address"' in body

    detail = client.get(reverse("trader_detail", args=[trader.pk]))
    assert detail.status_code == 200
    dbody = detail.content.decode()
    assert "What they usually trade" in dbody
    assert "How DreamLens sees them" in dbody
    assert "Sample size" in dbody
    assert "observed trades" in dbody
    assert "Past performance does not guarantee future results." in dbody


@pytest.mark.django_db
def test_decision_receipt_structure(client, user, copy_relationship, source_trade):
    CopyExecution.objects.create(
        relationship=copy_relationship,
        source_trade=source_trade,
        status=CopyExecution.Status.EXECUTED,
        copy_score=86,
        score_json={"trader": 82, "event": 86, "market": 70},
        why_json=["Trader activity is high"],
        amount=Decimal("4"),
    )
    client.force_login(user)
    res = client.get("/following/activity/")
    assert res.status_code == 200
    body = res.content.decode()
    assert "Why?" in body
    assert "What did the agent see?" in body
    assert "What rules applied?" in body
    assert "What did the agent do?" in body
    assert "What happened?" in body


@pytest.mark.django_db
def test_portfolio_level_one_words(client):
    res = client.get("/portfolio/")
    assert res.status_code == 200
    body = res.content.decode()
    assert "Available" in body
    assert "In active events" in body
    assert "Potential payout" in body
    assert "Today's result" in body
    assert "liquidation" not in body.lower()
    assert "Unrealized" not in body
    assert "dl-pf-money" in body


@pytest.mark.django_db
def test_landing_tells_the_product_story(client):
    res = client.get("/")
    assert res.status_code == 200
    body = res.content.decode()
    # Hero: what DreamLens is, and that DreamDEX stays the venue.
    assert "Built on DreamDEX Event Contracts" in body
    assert "Making Event Contracts easier to understand, trade, and automate." in body
    assert "DreamDEX stays the execution venue. You stay the owner." in body
    # Problem: the questions a new user asks, framed as UX not blockchain problems.
    assert "Prediction markets can be difficult to navigate." in body
    for q in ("What exactly am I buying?", "What does YES mean?", "How much can I lose?", "What are other traders doing?"):
        assert q in body
    assert "These are not blockchain problems" in body
    # Flow
    assert "How it works" in body
    assert "Discover → Understand → Decide → Trade → Learn" in body
    assert "Users should know what they are trading before they trade it." in body
    for step in ("Discover", "Understand", "Decide", "Trade", "Learn"):
        assert f'<span class="dl-landing-flow__name">{step}</span>' in body
    # Prices and payout math stay in dollars.
    assert "$0.41" in body
    assert "41¢" not in body
    assert "$12.20" in body
    # AI explains; the Score is a signal, not a probability.
    assert "82 / 100" in body
    assert SCORE_DISCLAIMER in body
    assert "82% chance of winning" in body  # quoted as the thing we do NOT say
    assert body.lower().count("chance of winning") == 1
    assert "AI provides context. The user makes the decision." in body
    # Smart Copy with caps
    assert "Smart Copy" in body
    assert "Controlled participation, not blind following." in body
    for cap in ("Maximum per trade", "Maximum daily allocation", "Minimum DreamLens Score"):
        assert cap in body
    # DreamAgent can / cannot
    assert "DreamAgent can" in body
    assert "DreamAgent cannot" in body
    assert "Withdraw your funds" in body
    assert "You can trade for me, but only within these rules." in body
    # Trade check + Agent check
    assert "Trade check" in body
    assert "Agent check" in body
    assert "The agent can make decisions. Your policies make the final rules." in body
    # Decision Receipt
    assert "The AI traded because these conditions were satisfied." in body
    assert "Decision Receipt" in body
    # Complexity, Telegram, architecture
    assert "Hide blockchain complexity. Never hide trading consequences." in body
    assert "Telegram is an interface, not a wallet." in body
    assert "Policy &amp; Risk Engine" in body
    assert "Intelligence on top. On-chain execution underneath." in body
    # Close
    assert "Understand. Decide. Trade. Automate." in body
    assert "Testnet only. No real monetary value." in body
    assert "Will Bitcoin be above $118,500 at expiry?" in body
    assert "Start trading" in body
    assert "yes-or-no" in body.lower()
    assert "ticker soup" not in body.lower()
    assert "watches the tape" not in body.lower()
    assert "Trading account" in body
    health = client.get("/healthz")
    assert health.status_code == 200
    assert health.content == b"ok"


@pytest.mark.django_db
def test_start_is_a_setup_info_page(client, user, sample_event):
    """/start/ explains the five steps; it no longer hosts the wizard controls."""
    for signed_in in (False, True):
        if signed_in:
            client.force_login(user)
        res = client.get("/start/")
        assert res.status_code == 200
        body = res.content.decode()
        assert "How to set up your Dream Agent" in body
        for label in ("Connect", "Trading account", "Add money", "Allow", "First $1"):
            assert label in body
        for title in (
            "Connect MetaMask",
            "Create your trading account",
            "Add money",
            "Allow DreamLens to trade for you",
            "Place a $1 YES or NO",
        ):
            assert title in body
        assert body.count('class="dl-setup__step"') == 5
        assert "Withdrawal: Never" in body
        assert "What you need" in body
        assert "Still your money" in body
        assert 'href="/agent/activate/"' in body
        assert 'href="/discover/"' in body
        assert "Look around first" in body
        # no wizard mechanics left on this page or in the shell
        for gone in (
            "data-onboarding",
            "dl-body--start",
            "data-start-connect",
            'id="sa-create"',
            'id="sa-deposit"',
            'id="grant-permission"',
            'data-amount="1"',
            "dl-start__",
        ):
            assert gone not in body, gone
        assert "Hybrid" not in body
        assert "EIP-712" not in body
        assert "session key" not in body.lower()
        # the normal app chrome stays visible on the info page
        assert "dl-nav" in body
    assert 'href="/start/"' in client.get("/").content.decode()


@pytest.mark.django_db
def test_first_session_done_names_the_ticket_and_stops_nudging(
    client, user, wallet, sample_event, settings
):
    settings.MOCK_SMART_ACCOUNT = True
    from apps.trading.models import Trade
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
    assert state["step"] == "done"
    assert state["title"] == "You bought YES."
    assert event_question(sample_event) in state["why"]
    assert "$0.41" in state["why"]
    assert "You're in" not in state["title"]
    assert "This trade is open" not in state["why"]
    assert state["why"].count("Claim") == 0
    assert state["incomplete"] is False
    assert state["next_url"] == "/portfolio/"

    client.force_login(user)
    home = client.get("/home/").content.decode()
    assert "Continue setup" not in home
    assert "dl-next-step" not in home
    # the info page is the same for everyone — no per-user wizard state
    body = client.get("/start/").content.decode()
    assert "How to set up your Dream Agent" in body
    assert "You bought YES." not in body


@pytest.mark.django_db
def test_event_detail_beginner_yes_line(client, sample_event):
    res = client.get(reverse("event_detail", args=[sample_event.pk]))
    assert res.status_code == 200
    body = res.content.decode()
    assert "YES = you think this happens. Price is what you pay now." in body
    modal = Path("templates/components/trade_modal.html").read_text()
    assert "YES = you think this happens. Price is what you pay now." in modal
    assert "Trading account ready" in modal
    assert "modal-tx-status" in modal
    assert "unsigned_tx" not in modal


@pytest.mark.django_db
def test_home_next_step_when_setup_incomplete(client, user, settings):
    settings.MOCK_SMART_ACCOUNT = True
    client.force_login(user)
    res = client.get("/home/")
    assert res.status_code == 200
    body = res.content.decode()
    assert "Continue setup" in body
    assert "Create your trading account" in body
    assert 'href="/agent/activate/"' in body
    assert "dl-nav-next" in body
    # Setup no longer hides the rest of Home — the nudge sits above a full page.
    assert "dl-home-desk__rail" in body
    assert 'href="/start/"' not in body


def test_claim_js_uses_sdk_gas_ceiling():
    from pathlib import Path

    js = Path("static/js/dreamlens.js").read_text()
    assert "fallbackGas" in js
    assert "10000000" in js
    assert "prepared.sync_tx" in js
    assert "prepared.claimed" in js
    assert "/api/portfolio/claim/" in js
    assert "data-claim-agent" in js
    assert "toast(err.message" in js
    assert "/api/agent/trade/" in js
    assert "/api/smart-account/withdraw/" in js
    assert "DreamLens is placing this trade" in js
    assert "Creating your trading account" in js
    assert "order: data.unsigned_tx" not in js


def test_no_native_popups_in_frontend_js():
    """alert()/confirm()/prompt() must not creep back — use toast()/confirmDialog()."""
    import re

    js = Path("static/js/dreamlens.js").read_text()
    native = re.findall(r"(?<![\w.])(?:window\.)?(alert|confirm|prompt)\(", js)
    assert native == [], f"native popups found: {native}"
    assert "function confirmDialog(" in js
    assert "function initUnfollowButtons(" in js
    assert '"/api/copy/" + encodeURIComponent(pk) + "/"' in js
    assert 'method: "DELETE"' in js


@pytest.mark.django_db
def test_unfollow_button_everywhere_a_followed_trader_appears(
    client, user, wallet, sample_event
):
    from django.core.cache import cache

    from apps.dreamcopy.models import CopyRelationship, TraderProfile, TraderTrade

    cache.clear()
    followed = TraderProfile.objects.create(
        wallet_address="0x6730d3a2a217108ab53ccfe60ffdad05d3c124e5",
        display_name="FollowedTrader",
        total_trades=4,
        total_volume=Decimal("12"),
    )
    stranger = TraderProfile.objects.create(
        wallet_address="0x1111111111111111111111111111111111111111",
        display_name="Stranger",
        total_trades=3,
        total_volume=Decimal("9"),
    )
    yes = sample_event.outcomes.get(outcome_type="YES")
    for i, t in enumerate((followed, stranger)):
        TraderTrade.objects.create(
            trader=t,
            event=sample_event,
            outcome=yes,
            entry_price=Decimal("0.41"),
            amount=Decimal("2"),
            opened_at=sample_event.expiry_time,
            external_trade_id=f"unf-fill-{i}",
        )
    rel = CopyRelationship.objects.create(
        user=user,
        trader=followed,
        status=CopyRelationship.Status.ACTIVE,
        copy_mode=CopyRelationship.CopyMode.SMART,
        max_per_trade=Decimal("5"),
        max_daily=Decimal("20"),
    )
    client.force_login(user)

    body = client.get("/following/").content.decode()
    unfollow = f'data-unfollow="{rel.pk}"'
    # chip row + people card + advanced table row
    assert body.count(unfollow) == 3
    assert f'data-follow-rel="{rel.pk}"' in body
    assert 'id="dl-confirm"' in body
    assert 'id="dl-confirm-ok"' in body
    # the stranger only gets Follow entry points
    stranger_card = body.split("Stranger", 1)[1].split("</article>", 1)[0]
    assert "data-unfollow" not in stranger_card
    assert "Follow" in stranger_card

    detail = client.get(reverse("trader_detail", args=[followed.pk])).content.decode()
    assert unfollow in detail
    assert 'data-unfollow-redirect="/following/"' in detail
    stranger_detail = client.get(reverse("trader_detail", args=[stranger.pk])).content.decode()
    assert "data-unfollow" not in stranger_detail

    portfolio = client.get("/portfolio/").content.decode()
    assert unfollow in portfolio
    assert "data-follow-count" in portfolio
    assert "data-follow-empty" in portfolio

    res = client.delete(f"/api/copy/{rel.pk}/")
    assert res.status_code in (200, 204)
    rel.refresh_from_db()
    assert rel.status == CopyRelationship.Status.STOPPED
    assert "data-unfollow" not in client.get("/following/").content.decode()


@pytest.mark.django_db
def test_trader_detail_is_an_activity_desk(client, sample_event):
    from django.core.cache import cache

    from apps.dreamcopy.models import TraderProfile, TraderTrade

    cache.clear()
    trader = TraderProfile.objects.create(
        wallet_address="0x6730d3a2a217108ab53ccfe60ffdad05d3c124e5",
        display_name="DeskTrader",
        total_trades=12,
        total_volume=Decimal("40"),
        trader_score=Decimal("0.72"),
        win_rate=Decimal("0.00"),
    )
    yes = sample_event.outcomes.get(outcome_type="YES")
    tx = "0x" + "ab" * 32
    TraderTrade.objects.create(
        trader=trader,
        event=sample_event,
        outcome=yes,
        entry_price=Decimal("0.41"),
        amount=Decimal("5"),
        opened_at=sample_event.expiry_time,
        transaction_hash=tx,
        external_trade_id="desk-fill-1",
    )
    res = client.get(reverse("trader_detail", args=[trader.pk]))
    assert res.status_code == 200
    body = res.content.decode()
    assert SCORE_DISCLAIMER in body
    assert "chance of winning" not in body.lower()
    assert "% profitable trades" not in body
    assert "Fill volume by day" in body
    assert "On-chain fills" in body
    assert "YES / NO split" in body
    assert "$0.41" in body
    assert "41¢" not in body
    assert tx[:10] in body
    assert f"/tx/{tx}" in body
    assert "dl-ta-kpis" in body
    assert "How DreamLens sees them" in body


@pytest.mark.django_db
def test_portfolio_desk_and_owner_withdraw(client, user, wallet, sample_event, settings):
    settings.MOCK_SMART_ACCOUNT = True
    from apps.trading.models import Trade
    from services import smart_account_service

    sa = smart_account_service.create_account(user, owner_address=wallet.address)
    smart_account_service.mark_funded(sa, amount=Decimal("50"))
    yes = sample_event.outcomes.get(outcome_type="YES")
    Trade.objects.create(
        user=user,
        event=sample_event,
        outcome=yes,
        side=Trade.Side.BUY,
        amount=Decimal("2"),
        entry_price=Decimal("0.41"),
        transaction_hash="0x" + "ef" * 32,
        status=Trade.Status.CONFIRMED,
    )
    client.force_login(user)
    res = client.get("/portfolio/")
    assert res.status_code == 200
    body = res.content.decode()
    assert "Wins in this book" in body
    assert "On-chain activity" in body
    assert "Withdraw to MetaMask" in body
    assert "Sends trading dollars from this account to the MetaMask that owns it." in body
    assert CLAIM_VS_CLOSE in body
    assert "chance of winning" not in body.lower()
    assert "$0.41" in body
    assert "41¢" not in body
    agent = client.get("/agent/")
    assert agent.status_code == 200
    agent_body = agent.content.decode()
    assert "Withdrawal: Never" in agent_body
    assert "Withdraw your funds" in agent_body
    assert "Withdraw to MetaMask" in agent_body
