"""Lens chat page and analyst API."""

from __future__ import annotations

import json

import pytest

from services.ai_service import DISCLAIMER, lens_chat


class _StubLLM:
    def __init__(self, title: str = "BTC"):
        self.title = title

    def complete(self, *, system, user, json_mode=False, **kwargs):
        assert "financial analyst" in system.lower() or "Lens" in system
        assert "Live DreamDEX book" in user
        assert kwargs.get("google_search") is True
        return (
            f"{self.title} is the most active live contract. "
            "Headlines around bitcoin can move YES prices quickly."
        )


@pytest.mark.django_db
def test_explore_has_no_search_box(client, sample_event):
    res = client.get("/explore/", follow=True)
    assert res.status_code == 200
    body = res.content.decode()
    assert 'id="ai-search-form"' not in body
    assert "Discover" in body


@pytest.mark.django_db
def test_lens_page_renders(client):
    res = client.get("/lens/")
    assert res.status_code == 200
    body = res.content.decode()
    assert 'id="lens-thread"' in body
    assert "News hitting BTC?" in body
    assert "financial analyst" in body.lower()
    assert "Live market feed" in body


@pytest.mark.django_db
def test_lens_chat_api_returns_insight_not_trade(client, sample_event, monkeypatch):
    stub = _StubLLM(sample_event.title)
    monkeypatch.setattr("services.ai_service.get_llm_client", lambda: stub)
    res = client.post(
        "/api/ai/lens/",
        data={"message": "What's moving right now?", "history": []},
        content_type="application/json",
    )
    assert res.status_code == 200
    body = res.json()
    assert body["intent"] == "LENS"
    assert body["reply"]
    assert "prepare_params" not in body
    assert "prepare_params" not in (body.get("tool_results") or {})
    assert DISCLAIMER in body["disclaimer"]
    events = body["tool_results"]["events"]
    assert events
    assert events[0]["id"] == sample_event.pk
    assert "title" in events[0]


@pytest.mark.django_db
def test_lens_chat_service_never_returns_prepare(sample_event, monkeypatch):
    monkeypatch.setattr("services.ai_service.get_llm_client", lambda: _StubLLM())
    result = lens_chat(message="News hitting BTC?", history=[])
    assert result["intent"] == "LENS"
    assert "prepare_params" not in result
    assert DISCLAIMER in result["reply"]


class _ExplainStub:
    def complete(self, *, system, user, json_mode=False, **kwargs):
        assert "You are Lens" in system
        assert json_mode is True
        assert kwargs.get("google_search") is False
        assert "Question:" in user
        assert "YES:" in user
        assert "Live headlines:" in user
        return json.dumps(
            {
                "setup": "YES is $0.50 with minutes left on a Bitcoin above-strike window.",
                "yes_needs": "YES needs the oracle print above the strike at expiry.",
                "no_needs": "NO wins at or below the strike when the window closes.",
                "in_the_price": "The book is split evenly, so neither side is paying a rich premium.",
                "could_change": "A Bitcoin headline or a last-minute print can reprice YES quickly.",
            }
        )


@pytest.mark.django_db
def test_structured_explain_returns_sections(client, sample_event, monkeypatch):
    monkeypatch.setattr("services.ai_service.get_llm_client", lambda: _ExplainStub())
    monkeypatch.setattr(
        "services.ai_service._headlines_for_asset",
        lambda *a, **k: [
            {
                "title": "Bitcoin holds above $80,000",
                "source": "CoinDesk",
                "ago": "12m ago",
                "url": "https://example.com/btc",
                "assets": ["BTC"],
            }
        ],
    )
    res = client.post(
        "/api/ai/lens/",
        data={
            "message": "Explain this market.",
            "history": [{"role": "user", "content": "Earlier context"}],
            "event_id": sample_event.pk,
            "structured": True,
        },
        content_type="application/json",
    )
    assert res.status_code == 200
    body = res.json()
    assert body["intent"] == "LENS"
    assert body["explanation"]["yes_needs"]
    assert "YES needs" in body["reply"]
    assert "prepare_params" not in (body.get("tool_results") or {})
    assert DISCLAIMER in body["reply"]


def test_explanation_flattens_nested_objects():
    from services.ai_service import _explanation_from_llm

    sections = _explanation_from_llm(
        {
            "setup": {
                "question": "Will Bitcoin be above $80,273.30 at expiry?",
                "current YES/NO": "YES: $0.72, NO: $0.28",
            },
            "yes_needs": {"what must happen": "Bitcoin must finish above the strike."},
            "no_needs": "NO wins at or below the strike.",
        }
    )
    assert "Will Bitcoin be above $80,273.30" in sections["setup"]
    assert "{" not in sections["setup"]
    assert "Bitcoin must finish above the strike." in sections["yes_needs"]
