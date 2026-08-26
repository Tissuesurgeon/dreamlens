"""AI service structured output and intent parsing."""

from __future__ import annotations

from decimal import Decimal

import pytest

from services.ai_service import analyze_event, parse_intent

REQUIRED_ANALYZE_KEYS = {
    "event_id",
    "title",
    "estimated_probability",
    "market_probability",
    "confidence",
    "signal",
    "reasons",
    "risks",
    "label",
    "disclaimer",
}


@pytest.mark.django_db
def test_analyze_event_has_required_keys(sample_event, user):
    result = analyze_event(sample_event, user=user)
    assert REQUIRED_ANALYZE_KEYS.issubset(result.keys())
    assert isinstance(result["reasons"], list)
    assert isinstance(result["risks"], list)
    assert result["disclaimer"]


def test_parse_intent_btc_search():
    parsed = parse_intent("find BTC events")
    assert parsed.intent == "SEARCH_EVENTS"
    assert parsed.params.get("asset") == "BTC"
    assert "event_id" not in parsed.params
    assert "id" not in parsed.params


def test_parse_intent_ask_chips():
    interesting = parse_intent("What's interesting right now?")
    assert interesting.intent == "SEARCH_EVENTS"
    assert interesting.params.get("sort") == "interesting"

    btc = parse_intent("Show me BTC markets")
    assert btc.intent == "SEARCH_EVENTS"
    assert btc.params.get("asset") == "BTC"

    soon = parse_intent("Markets ending soon")
    assert soon.intent == "SEARCH_EVENTS"
    assert soon.params.get("sort") == "expiry"


@pytest.mark.django_db
def test_chat_api_accepts_query_alias(client):
    res = client.post(
        "/api/ai/chat/",
        data={"query": "Show me BTC markets"},
        content_type="application/json",
    )
    assert res.status_code == 200
    body = res.json()
    assert body.get("reply")
    assert body.get("intent") == "SEARCH_EVENTS"


def test_parse_intent_prepare_trade_without_inventing_ids():
    parsed = parse_intent("place $25 yes")
    assert parsed.intent == "PREPARE_TRADE"
    assert parsed.params["amount"] == Decimal("25")
    assert parsed.params["outcome"] == "YES"
    assert "event_id" not in parsed.params
    assert "market_id" not in parsed.params


def test_parse_intent_prepare_trade_captures_asset_not_id():
    parsed = parse_intent("Buy $5 YES on BTC")
    assert parsed.intent == "PREPARE_TRADE"
    assert parsed.params["amount"] == Decimal("5")
    assert parsed.params["outcome"] == "YES"
    assert parsed.params["asset"] == "BTC"
    assert "event_id" not in parsed.params
