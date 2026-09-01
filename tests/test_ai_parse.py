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
    "setup",
}


@pytest.mark.django_db
def test_analyze_event_has_required_keys(sample_event, user):
    result = analyze_event(sample_event, user=user)
    assert REQUIRED_ANALYZE_KEYS.issubset(result.keys())
    assert isinstance(result["reasons"], list)
    assert isinstance(result["risks"], list)
    assert len(result["reasons"]) >= 4
    assert len(result["risks"]) >= 3
    assert result["setup"]
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


def test_get_llm_client_defaults_to_google_gemini(settings):
    from services.ai_service import CascadingLLMClient, GoogleAIStudioClient, get_llm_client

    settings.LOCAL_LLM_ENABLED = False
    settings.OPENROUTER_API_KEY = ""
    settings.GEMINI_API_KEY = "aq-test"
    settings.LLM_API_KEY = ""
    settings.LLM_PROVIDER = "google"
    settings.LLM_MODEL = "gemini-3.7-flash"
    settings.LLM_BASE_URL = "https://generativelanguage.googleapis.com/v1beta/openai"
    client = get_llm_client()
    assert isinstance(client, CascadingLLMClient)
    primary = client.clients[0]
    assert isinstance(primary, GoogleAIStudioClient)
    assert primary.model == "gemini-3.7-flash"
    assert primary.label == "google"


def test_gemini_model_does_not_send_google_key_to_openrouter(settings):
    from services.ai_service import CascadingLLMClient, GoogleAIStudioClient, get_llm_client

    settings.LOCAL_LLM_ENABLED = False
    settings.LLM_PROVIDER = "openrouter"
    settings.LLM_BASE_URL = "https://openrouter.ai/api/v1"
    settings.LLM_MODEL = "gemini-3.7-flash"
    settings.LLM_API_KEY = "AQ.test-google-key"
    settings.GEMINI_API_KEY = "AQ.test-google-key"
    settings.OPENROUTER_API_KEY = "sk-or-test"
    client = get_llm_client()
    assert isinstance(client, CascadingLLMClient)
    primary = client.clients[0]
    assert isinstance(primary, GoogleAIStudioClient)
    assert primary.api_key.startswith("AQ.")
    assert primary.label == "google"


def test_get_llm_client_uses_lan_ollama_when_provider_is_ollama(settings):
    from services.ai_service import CascadingLLMClient, OpenAICompatibleClient, get_llm_client

    settings.LOCAL_LLM_ENABLED = True
    settings.LLM_PROVIDER = "ollama"
    settings.LLM_MODEL = "llama3.2"
    settings.LLM_BASE_URL = "http://192.168.0.110:11434/v1"
    settings.LLM_API_KEY = "local"
    settings.LOCAL_LLM_BASE_URL = "http://192.168.0.110:11434/v1"
    settings.LOCAL_LLM_MODEL = "llama3.2"
    settings.LOCAL_LLM_API_KEY = "local"
    settings.GEMINI_API_KEY = "AQ.should-not-win"
    client = get_llm_client()
    assert isinstance(client, CascadingLLMClient)
    primary = client.clients[0]
    assert isinstance(primary, OpenAICompatibleClient)
    assert primary.label == "ollama"
    assert primary.model == "llama3.2"
    assert "192.168.0.110:11434" in (primary.base_url or "")


def test_get_llm_client_uses_openrouter_ling_flash(settings):
    from services.ai_service import CascadingLLMClient, OpenAICompatibleClient, get_llm_client

    settings.LOCAL_LLM_ENABLED = False
    settings.LLM_PROVIDER = "openrouter"
    settings.LLM_MODEL = "inclusionai/ling-3.0-flash-fin:free"
    settings.LLM_BASE_URL = "https://openrouter.ai/api/v1"
    settings.LLM_API_KEY = ""
    settings.OPENROUTER_API_KEY = "sk-or-test"
    settings.GEMINI_API_KEY = "AQ.should-not-win"
    settings.LLM_REASONING = True
    client = get_llm_client()
    assert isinstance(client, CascadingLLMClient)
    primary = client.clients[0]
    assert isinstance(primary, OpenAICompatibleClient)
    assert primary.label == "openrouter"
    assert primary.model == "inclusionai/ling-3.0-flash-fin:free"
    assert primary.extra_body.get("reasoning") == {"enabled": True}


def test_get_llm_client_uses_cursor_composer(settings):
    from services.ai_service import CascadingLLMClient, CursorLLMClient, get_llm_client

    settings.LOCAL_LLM_ENABLED = False
    settings.LLM_PROVIDER = "cursor"
    settings.LLM_MODEL = "composer-2.5"
    settings.LLM_BASE_URL = "https://api.cursor.com/v1"
    settings.LLM_API_KEY = ""
    settings.CURSOR_API_KEY = "crsr_testkey"
    settings.OPENROUTER_API_KEY = "sk-or-should-not-win"
    settings.GEMINI_API_KEY = "AQ.should-not-win"
    client = get_llm_client()
    assert isinstance(client, CascadingLLMClient)
    primary = client.clients[0]
    assert isinstance(primary, CursorLLMClient)
    assert primary.label == "cursor"
    assert primary.model == "composer-2.5"
    assert primary.base_url == "https://api.cursor.com/v1"


def test_cascade_skips_empty_openrouter_and_uses_local():
    from services.ai_service import CascadingLLMClient

    class EmptyPrimary:
        label = "openrouter"

        def complete(self, **kwargs):
            return "{}"

    class LocalOk:
        label = "local"

        def complete(self, **kwargs):
            return '{"ok": true}'

    client = CascadingLLMClient([EmptyPrimary(), LocalOk()])
    assert client.complete(system="s", user="u") == '{"ok": true}'
