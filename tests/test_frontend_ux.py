"""User-facing frontend: language helpers, IA, receipts, trade copy."""

from __future__ import annotations

from decimal import Decimal

import pytest
from django.urls import reverse

from apps.dreamcopy.models import CopyExecution
from apps.events.models import EventContract
from services.event_copy import (
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


@pytest.mark.django_db
def test_landing_tells_the_product_story(client):
    res = client.get("/")
    assert res.status_code == 200
    body = res.content.decode()
    assert "Understand. Decide. Trade with Confidence." in body
    assert "How it works" in body
    assert "From looking to a Decision Receipt." in body
    assert "$0.41" in body
    assert "41¢" not in body
    assert "82 / 100" in body
    assert SCORE_DISCLAIMER in body
    assert "chance of winning" not in body.lower()
    assert "Testnet only. No real monetary value." in body
    assert "Will Bitcoin be above $118,500 at expiry?" in body
    assert "DreamAgent cannot" in body
    assert "Intelligence on top. On-chain execution underneath." in body
    health = client.get("/healthz")
    assert health.status_code == 200
    assert health.content == b"ok"


def test_claim_js_uses_sdk_gas_ceiling():
    from pathlib import Path

    js = Path("static/js/dreamlens.js").read_text()
    assert "fallbackGas" in js
    assert "10000000" in js
    assert "prepared.sync_tx" in js
    assert "prepared.claimed" in js
    assert "/api/portfolio/claim/" in js
    assert "data-claim-agent" in js
