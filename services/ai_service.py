"""Provider-agnostic AI lens — estimates only, never guarantees."""

from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Protocol, runtime_checkable

from django.conf import settings
from django.core.cache import cache

from apps.agents.models import AgentDecision
from apps.dreamcopy.models import CopyRelationship
from apps.events.models import EventContract, EventOutcome

logger = logging.getLogger("dreamlens.services.ai")

DISCLAIMER = "DreamLens estimate — not financial advice or a guaranteed outcome."
FOUR_PLACES = Decimal("0.0001")


def _as_str_list(value: Any, fallback: str | list[str], *, limit: int = 8) -> list[str]:
    fallbacks = [fallback] if isinstance(fallback, str) else [str(x) for x in fallback if str(x).strip()]
    items: list[str] = []
    if isinstance(value, str) and value.strip():
        items = [value.strip()]
    elif isinstance(value, list):
        for item in value:
            if isinstance(item, str) and item.strip():
                items.append(item.strip())
            elif isinstance(item, dict):
                text = item.get("text") or item.get("reason") or item.get("message")
                if text:
                    items.append(str(text).strip())
    if items:
        return items[:limit]
    return fallbacks[:limit] if fallbacks else ["Insufficient detail from the model."]


def _parse_llm_object(raw: str, fallback: dict[str, Any] | None = None) -> dict[str, Any]:
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return dict(fallback or {})
    if isinstance(data, list):
        data = data[0] if data and isinstance(data[0], dict) else {}
    if not isinstance(data, dict):
        return dict(fallback or {})
    return data

_rate_limit: dict[str, float] = {}
RATE_LIMIT_SECONDS = 1.0


@runtime_checkable
class LLMClient(Protocol):
    def complete(
        self,
        *,
        system: str,
        user: str,
        json_mode: bool = False,
        **kwargs: Any,
    ) -> str: ...


class MockLLMClient:
    """Deterministic mock responses when no API key is configured."""

    def complete(self, *, system: str, user: str, json_mode: bool = False, **kwargs: Any) -> str:
        blob = f"{system}\n{user}".lower()
        if json_mode and ("yes_needs" in blob or "in_the_price" in blob):
            return json.dumps(
                {
                    "setup": (
                        "This is a short binary window on whether the underlying finishes "
                        "above the listed strike by expiry. YES is the claim that it does; "
                        "NO is the claim that it does not. Current YES and NO prices already "
                        "split that claim into two complementary sides."
                    ),
                    "yes_needs": (
                        "YES wins only if the oracle print at expiry is strictly above the strike. "
                        "A last-minute rally that fades before settlement still resolves NO. "
                        "Watch both the distance to strike and the minutes remaining."
                    ),
                    "no_needs": (
                        "NO wins if the underlying is at or below the strike when the window closes. "
                        "Holding near the strike into the final minutes favors NO because the "
                        "question is 'above', not 'at'."
                    ),
                    "in_the_price": (
                        "The YES price is what traders are paying for that claim right now, not a "
                        "guaranteed chance of winning. Volume and trade count show how much "
                        "conviction is actually in the book."
                    ),
                    "could_change": (
                        "A sharp move in the underlying, thin liquidity near expiry, or a headline "
                        "that reprices BTC/ETH can flip YES and NO quickly. Oracle timing can "
                        "differ from the spot ticker you are watching."
                    ),
                }
            )
        if json_mode and (
            "estimated_probability" in blob
            or "senior-desk" in blob
            or "analyze binary" in blob
        ):
            return json.dumps(
                {
                    "estimated_probability": 0.58,
                    "market_probability": 0.52,
                    "confidence": 0.62,
                    "signal": "LEAN_YES",
                    "setup": (
                        "The market is pricing a modest YES lean with limited time left. "
                        "DreamLens is comparing that book to volume, trade count, and the "
                        "distance between the underlying and the strike — not calling a winner."
                    ),
                    "reasons": [
                        "YES is trading above 0.50, so the book already leans that the underlying clears the strike.",
                        "Volume is high enough that the price is not a single fill.",
                        "Trade count shows more than one participant is active in this window.",
                        "Time remaining still allows a move, but not a slow grind from far below the strike.",
                        "Supplied headlines, if any, are consistent with the current lean rather than a shock.",
                    ],
                    "risks": [
                        "Short expiry increases the chance of a last-print reversal.",
                        "Liquidity can thin in the final minutes, so a small order moves YES a long way.",
                        "Oracle settlement can differ from the spot ticker on your screen.",
                        "A headline that hits after you look can reprice the book before expiry.",
                    ],
                    "label": "DreamLens estimate",
                    "disclaimer": DISCLAIMER,
                }
            )
        if "you are lens" in system.lower() or "financial analyst" in system.lower():
            return (
                "Live DreamDEX markets look mixed. The busiest contract is the one with the "
                "highest volume and the shortest time left — treat that as flow, not a forecast. "
                "Headlines around bitcoin and ether can reprice YES within a single window, "
                "especially when the underlying is close to the strike. "
                + DISCLAIMER
            )
        if "copy" in user.lower():
            return json.dumps(
                {
                    "decision": "COPY",
                    "confidence": 0.68,
                    "reasoning": "Trader history and market alignment support copy",
                }
            )
        return json.dumps(
            {
                "intent": "CHAT",
                "message": "I can help you search events, analyze markets, or prepare trades.",
            }
        )


class OpenAICompatibleClient:
    """OpenAI-compatible chat completions API. Raises on transport/API failure."""

    def __init__(
        self,
        *,
        api_key: str,
        model: str,
        base_url: str = "https://generativelanguage.googleapis.com/v1beta/openai",
        label: str = "primary",
        timeout: float = 60,
        extra_headers: dict[str, str] | None = None,
        extra_body: dict[str, Any] | None = None,
    ) -> None:
        self.api_key = api_key
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.label = label
        self.timeout = timeout
        self.extra_headers = extra_headers or {}
        self.extra_body = extra_body or {}

    def complete(self, *, system: str, user: str, json_mode: bool = False, **kwargs: Any) -> str:
        import urllib.error
        import urllib.request

        def _request(use_json_mode: bool) -> str:
            payload: dict[str, Any] = {
                "model": self.model,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                "temperature": 0.2,
            }
            max_tokens = int(kwargs.get("max_output_tokens") or 0)
            if max_tokens > 0:
                payload["max_tokens"] = max_tokens
            if use_json_mode:
                payload["response_format"] = {"type": "json_object"}
            if self.extra_body:
                payload.update(self.extra_body)

            req = urllib.request.Request(
                f"{self.base_url}/chat/completions",
                data=json.dumps(payload).encode(),
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                    **self.extra_headers,
                },
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                body = json.loads(resp.read().decode())
            choices = body.get("choices", [])
            if not choices:
                return "{}"
            return _strip_thoughts(choices[0].get("message", {}).get("content", "{}"))

        try:
            return _request(json_mode)
        except urllib.error.HTTPError as exc:
            detail = _http_error_detail(exc)
            # Many local servers reject response_format — retry without it.
            if json_mode and exc.code in (400, 422):
                logger.info("%s LLM rejected json_mode; retrying plain", self.label)
                try:
                    return _request(False)
                except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as retry_exc:
                    raise RuntimeError(f"{self.label} LLM failed: {retry_exc}") from retry_exc
            raise RuntimeError(f"{self.label} LLM HTTP {exc.code}: {detail}") from exc
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"{self.label} LLM failed: {exc}") from exc


def _strip_thoughts(text: str) -> str:
    cleaned = re.sub(r"<thought>.*?</thought>", "", text or "", flags=re.DOTALL | re.I)
    cleaned = cleaned.strip()
    return cleaned or (text or "{}")


def _http_error_detail(exc: Exception) -> str:
    reason = getattr(exc, "reason", "") or type(exc).__name__
    body = ""
    reader = getattr(exc, "read", None)
    if callable(reader):
        try:
            body = reader().decode("utf-8", errors="replace")[:240]
        except Exception:  # noqa: BLE001
            body = ""
    body = re.sub(r"AQ\.[A-Za-z0-9*]+", "AQ.[redacted]", body)
    body = re.sub(r"AIza[A-Za-z0-9_-]+", "AIza[redacted]", body)
    body = re.sub(r"sk-or-v1-[A-Za-z0-9*]+", "sk-or-[redacted]", body)
    return f"{reason}: {body}" if body else str(reason)


class GoogleAIStudioClient:
    """Gemini generateContent API. Keeps thinking at minimal for interactive latency."""

    def __init__(
        self,
        *,
        api_key: str,
        model: str,
        label: str = "google",
        timeout: float = 60,
    ) -> None:
        self.api_key = api_key
        self.model = model.removeprefix("models/")
        self.label = label
        self.timeout = timeout
        self.base_url = "https://generativelanguage.googleapis.com/v1beta"

    def complete(self, *, system: str, user: str, json_mode: bool = False, **kwargs: Any) -> str:
        import urllib.error
        import urllib.request

        history = kwargs.get("history") or []
        google_search = bool(kwargs.get("google_search"))

        def _contents() -> list[dict[str, Any]]:
            turns: list[dict[str, Any]] = []
            for item in history:
                if not isinstance(item, dict):
                    continue
                text = str(item.get("content") or item.get("text") or "").strip()
                role = str(item.get("role") or "user").lower()
                if not text:
                    continue
                gem_role = "model" if role in {"assistant", "model"} else "user"
                turns.append({"role": gem_role, "parts": [{"text": text}]})
            turns.append({"role": "user", "parts": [{"text": user}]})
            return turns

        def _request(use_json_mode: bool, use_search: bool) -> str:
            generation_config: dict[str, Any] = {
                "maxOutputTokens": int(kwargs.get("max_output_tokens") or 1024),
                "thinkingConfig": _google_thinking_config(self.model),
            }
            # Gemini 3.x rejects/ignores sampling knobs; Gemma still uses them.
            if not self.model.lower().startswith("gemini-3"):
                generation_config["temperature"] = 0.2
            if use_json_mode:
                generation_config["responseMimeType"] = "application/json"
            payload: dict[str, Any] = {
                "systemInstruction": {"parts": [{"text": system}]},
                "contents": _contents(),
                "generationConfig": generation_config,
            }
            if use_search:
                payload["tools"] = [{"google_search": {}}]
            req = urllib.request.Request(
                f"{self.base_url}/models/{self.model}:generateContent",
                data=json.dumps(payload).encode(),
                headers={
                    "Content-Type": "application/json",
                    "x-goog-api-key": self.api_key,
                },
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                body = json.loads(resp.read().decode())
            return _strip_thoughts(_extract_gemini_text(body))

        try:
            return _request(json_mode, google_search)
        except urllib.error.HTTPError as exc:
            detail = _http_error_detail(exc)
            if google_search and exc.code in (400, 404, 422):
                logger.info("%s LLM rejected google_search; retrying without", self.label)
                try:
                    return _request(json_mode, False)
                except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, json.JSONDecodeError) as retry_exc:
                    raise RuntimeError(f"{self.label} LLM failed: {retry_exc}") from retry_exc
            if json_mode and exc.code in (400, 422):
                logger.info("%s LLM rejected json_mode; retrying plain", self.label)
                try:
                    return _request(False, False)
                except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as retry_exc:
                    raise RuntimeError(f"{self.label} LLM failed: {retry_exc}") from retry_exc
            raise RuntimeError(f"{self.label} LLM HTTP {exc.code}: {detail}") from exc
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"{self.label} LLM failed: {exc}") from exc


def _google_thinking_config(model: str) -> dict[str, Any]:
    """Gemma 4 only accepts minimal/high. Gemini 3.7 Flash rejects MINIMAL."""
    if "gemma-4" in (model or "").lower():
        return {"thinkingLevel": "minimal"}
    return {"thinkingLevel": "low"}


def _extract_gemini_text(body: dict[str, Any]) -> str:
    candidates = body.get("candidates") or []
    if not candidates:
        return "{}"
    parts = (candidates[0].get("content") or {}).get("parts") or []
    chunks: list[str] = []
    for part in parts:
        if part.get("thought"):
            continue
        text = part.get("text")
        if text:
            chunks.append(text)
    return "".join(chunks).strip() or "{}"


class UnavailableLLMClient:
    """Explicit failure — never invent market probabilities."""

    def complete(self, *, system: str, user: str, json_mode: bool = False, **kwargs: Any) -> str:
        return json.dumps(
            {
                "available": False,
                "decision": "SKIP",
                "confidence": 0,
                "signal": "UNAVAILABLE",
                "reasons": ["AI analysis is unavailable — no LLM provider responded."],
                "risks": ["Proceed from on-chain market data only."],
                "reasoning": "AI unavailable",
                "disclaimer": DISCLAIMER,
            }
        )


class CascadingLLMClient:
    """Try clients in order. Tests may fall back to MockLLM; live does not."""

    def __init__(self, clients: list[LLMClient]) -> None:
        self.clients = clients

    def complete(self, *, system: str, user: str, json_mode: bool = False, **kwargs: Any) -> str:
        for client in self.clients:
            label = getattr(client, "label", client.__class__.__name__)
            try:
                return client.complete(system=system, user=user, json_mode=json_mode, **kwargs)
            except Exception as exc:  # noqa: BLE001 — cascade to next provider
                logger.warning("LLM provider %s failed: %s", label, exc)
        if getattr(settings, "MOCK_DREAMDEX", False):
            return MockLLMClient().complete(system=system, user=user, json_mode=json_mode, **kwargs)
        return UnavailableLLMClient().complete(system=system, user=user, json_mode=json_mode, **kwargs)


def _local_llm_client() -> OpenAICompatibleClient | None:
    if not getattr(settings, "LOCAL_LLM_ENABLED", False):
        return None
    return OpenAICompatibleClient(
        api_key=settings.LOCAL_LLM_API_KEY or "local",
        model=settings.LOCAL_LLM_MODEL or "llama3.2",
        base_url=settings.LOCAL_LLM_BASE_URL or "http://127.0.0.1:11434/v1",
        label="local",
        timeout=60,
    )


def _is_google_model(model: str) -> bool:
    name = (model or "").lower().removeprefix("models/")
    return name.startswith("gemini-") or name.startswith("gemma-")


def _is_google_key(key: str) -> bool:
    return (key or "").startswith(("AQ.", "AIza"))


def _is_google_llm() -> bool:
    provider = (getattr(settings, "LLM_PROVIDER", "") or "").lower()
    base = (getattr(settings, "LLM_BASE_URL", "") or "").lower()
    model = getattr(settings, "LLM_MODEL", "") or ""
    return (
        provider in {"google", "gemini"}
        or "generativelanguage.googleapis.com" in base
        or _is_google_model(model)
    )


def _google_api_key() -> str:
    gemini = getattr(settings, "GEMINI_API_KEY", "") or ""
    llm = getattr(settings, "LLM_API_KEY", "") or ""
    if _is_google_key(gemini):
        return gemini
    if _is_google_key(llm):
        return llm
    return gemini or (llm if not llm.startswith("sk-") else "")


def _openrouter_api_key() -> str:
    or_key = getattr(settings, "OPENROUTER_API_KEY", "") or ""
    llm = getattr(settings, "LLM_API_KEY", "") or ""
    if llm.startswith("sk-or-"):
        return llm
    if _is_google_key(llm):
        return or_key
    return or_key or llm


def _openrouter_headers() -> dict[str, str]:
    base = (getattr(settings, "LLM_BASE_URL", "") or "").lower()
    provider = (getattr(settings, "LLM_PROVIDER", "") or "").lower()
    if "openrouter.ai" not in base and provider != "openrouter":
        return {}
    headers: dict[str, str] = {}
    referer = (getattr(settings, "LLM_HTTP_REFERER", "") or "").strip()
    title = (getattr(settings, "LLM_APP_TITLE", "") or "").strip()
    if referer:
        headers["HTTP-Referer"] = referer
    if title:
        headers["X-Title"] = title
    return headers


def get_llm_client() -> LLMClient:
    """Primary cloud LLM → local Ollama/LM Studio → mock."""
    chain: list[LLMClient] = []
    model = settings.LLM_MODEL or "gemini-3.7-flash"

    if _is_google_llm():
        google_key = _google_api_key()
        if google_key:
            chain.append(
                GoogleAIStudioClient(
                    api_key=google_key,
                    model=model,
                    label="google",
                )
            )
    else:
        or_key = _openrouter_api_key()
        if or_key:
            chain.append(
                OpenAICompatibleClient(
                    api_key=or_key,
                    model=model,
                    base_url=getattr(settings, "LLM_BASE_URL", None)
                    or "https://openrouter.ai/api/v1",
                    label="openrouter",
                    extra_headers=_openrouter_headers(),
                )
            )

    local = _local_llm_client()
    if local is not None:
        chain.append(local)

    if not chain:
        if getattr(settings, "MOCK_DREAMDEX", False):
            return MockLLMClient()
        return UnavailableLLMClient()
    return CascadingLLMClient(chain)


def _rate_limit_key(user_id: int | None) -> str:
    return f"user:{user_id or 'anon'}"


def _check_rate_limit(user_id: int | None) -> None:
    key = _rate_limit_key(user_id)
    now = time.monotonic()
    last = _rate_limit.get(key, 0)
    if now - last < RATE_LIMIT_SECONDS:
        time.sleep(RATE_LIMIT_SECONDS - (now - last))
    _rate_limit[key] = time.monotonic()


def _headlines_for_asset(asset: str | None, *, limit: int = 8) -> list[dict]:
    try:
        from services.market_news import list_headlines

        return list_headlines(asset=asset, limit=limit)
    except Exception:
        logger.warning("headline fetch failed for AI brief asset=%s", asset, exc_info=True)
        return []


def _radar_labels(event: EventContract) -> list[str]:
    types = getattr(event, "radar_types", None)
    if types:
        return [str(t).replace("_", " ").title() for t in types]
    signals = getattr(event, "radar_signals", None)
    if signals is None:
        return []
    rows = signals.all() if hasattr(signals, "all") else signals
    labels = []
    for signal in rows:
        if getattr(signal, "is_active", True):
            labels.append(str(signal.signal_type).replace("_", " ").title())
    return labels


def _event_analyst_brief(event: EventContract, *, headlines: list[dict] | None = None) -> str:
    """Dense, numbered context so explanations can cite the actual book."""
    from services.event_copy import (
        as_cents,
        asset_display_name,
        dreamlens_score,
        event_question,
        event_strike_usd,
        format_collateral,
        format_ends_in,
        minutes_left,
        yes_no_outcomes,
    )
    from services.market_news import format_headlines_for_prompt

    yes, no = yes_no_outcomes(event)
    score = getattr(event, "dl_score", None) or dreamlens_score(event)
    strike = event_strike_usd(event)
    mins = minutes_left(event)
    window_min = max(1, int((getattr(event, "interval_sec", 0) or 900) / 60))
    desc = (getattr(event, "description", None) or "").strip()
    if len(desc) > 280:
        desc = desc[:277] + "…"
    news = headlines if headlines is not None else _headlines_for_asset(event.underlying_asset)
    lines = [
        f"Question: {event_question(event)}",
        f"Asset: {asset_display_name(event.underlying_asset)} ({event.underlying_asset})",
        f"Strike / opening reference: {format_collateral(strike) if strike is not None else 'not indexed'}",
        f"YES: {as_cents(yes.current_price if yes else None)}  NO: {as_cents(no.current_price if no else None)}",
        f"Volume: {format_collateral(event.cumulative_quote_volume)}  Trades: {int(event.trade_count or 0)}",
        f"Window: {window_min}-minute contract  Time left: {format_ends_in(event)} ({mins:.1f} minutes)",
        (
            f"DreamLens Score: {score.get('score')}/100 "
            f"(activity {score.get('activity')}, liquidity {score.get('liquidity')}, "
            f"trader activity {score.get('trader_activity')}, time {score.get('time_remaining')}). "
            "This score is an analysis signal, never a probability of winning."
        ),
        f"Radar: {', '.join(_radar_labels(event)) or 'none active'}",
    ]
    if desc:
        lines.append(f"Description: {desc}")
    lines.append("Live headlines:")
    lines.append(format_headlines_for_prompt(news))
    return "\n".join(lines)


def analyze_event(event: EventContract, *, user=None) -> dict:
    """Return structured DreamLens estimate for an event."""
    headlines = _headlines_for_asset(event.underlying_asset, limit=6)
    news_sig = "|".join((row.get("title") or "")[:40] for row in headlines[:3])
    cache_key = (
        f"ai:analyze:{event.pk}:{event.last_price}:{event.cumulative_quote_volume}:{hash(news_sig)}"
    )
    if user is None:
        cached = cache.get(cache_key)
        if cached is not None:
            return cached

    _check_rate_limit(user.pk if user else None)

    yes = event.outcomes.filter(outcome_type=EventOutcome.OutcomeType.YES).first()
    market_prob = float(yes.current_price) if yes else 0.5
    brief = _event_analyst_brief(event, headlines=headlines)

    client = get_llm_client()
    prompt = (
        "Write a senior-desk analysis of this live DreamDEX binary event.\n"
        "Use only the brief below. Cite YES/NO prices, strike, volume, trades, and time left. "
        "If headlines are listed, say how they could move this window; do not invent news.\n\n"
        f"{brief}\n\n"
        "Respond with JSON keys:\n"
        "- setup: 3–5 sentences on what the question is and what the book is doing now\n"
        "- estimated_probability: 0-1 analytical estimate of YES, not a chance of winning\n"
        "- market_probability: current YES price as 0-1\n"
        "- confidence: 0-1 how much the book + headlines support that estimate\n"
        "- signal: LEAN_YES | LEAN_NO | NEUTRAL | INTERESTING\n"
        "- reasons: exactly 5 strings, each citing a number or headline from the brief\n"
        "- risks: exactly 4 strings, each a concrete way this window can reverse\n"
        "Never recommend a trade. Never call DreamLens Score a probability."
    )
    raw = client.complete(
        system=(
            "You are DreamLens AI, a careful event-contract analyst. "
            "Estimates only — never guarantees, never buy/sell instructions."
        ),
        user=prompt,
        json_mode=True,
        max_output_tokens=2048,
    )
    data = _parse_llm_object(raw)

    result = {
        "event_id": event.pk,
        "title": event.title,
        "setup": _as_prose(data.get("setup"))
        or (
            f"YES is {market_prob:.2f} with {event.minutes_to_expiry:.1f} minutes left. "
            "This is the market price, not a guaranteed outcome."
        ),
        "estimated_probability": Decimal(str(data.get("estimated_probability", market_prob + 0.03))).quantize(
            FOUR_PLACES
        ),
        "market_probability": Decimal(str(data.get("market_probability", market_prob))).quantize(FOUR_PLACES),
        "confidence": Decimal(str(data.get("confidence", 0.55))).quantize(FOUR_PLACES),
        "signal": data.get("signal", "NEUTRAL"),
        "reasons": _as_str_list(
            data.get("reasons"),
            [
                f"YES is trading at {market_prob:.2f} — that is the live book, not a forecast.",
                f"Volume is {event.cumulative_quote_volume} with {event.trade_count} trades in this window.",
                f"{event.minutes_to_expiry:.1f} minutes remain, so a last-print move can still matter.",
                "DreamLens Score is an activity/liquidity signal, not a chance of winning.",
                "Headlines can reprice the underlying faster than this window can absorb.",
            ],
            limit=5,
        ),
        "risks": _as_str_list(
            data.get("risks"),
            [
                "Short expiry — price can reverse in one print.",
                "Liquidity may thin near settlement, so a small fill moves YES a long way.",
                "Oracle timing can differ from the spot ticker on screen.",
                "A headline after you look can reprice BTC/ETH before expiry.",
            ],
            limit=4,
        ),
        "label": "DreamLens estimate",
        "disclaimer": DISCLAIMER,
    }

    if user:
        AgentDecision.objects.create(
            user=user,
            event=event,
            action="ANALYZE_EVENT",
            confidence=result["confidence"],
            reasoning="; ".join(result["reasons"][:3]),
            structured_output_json={k: str(v) if isinstance(v, Decimal) else v for k, v in result.items()},
        )
    else:
        cache.set(cache_key, result, timeout=120)

    return result


def evaluate_copy(
    *,
    event: EventContract,
    source_trade,
    relationship: CopyRelationship,
) -> dict:
    """SMART copy decision — advisory; risk engine has final say."""
    client = get_llm_client()
    prompt = (
        f"Copy decision for {relationship.copy_mode} mode.\n"
        f"Event: {event.title}\n"
        f"Side: {source_trade.outcome.outcome_type}\n"
        f"Amount: {source_trade.amount}\n"
        f"Trader score: {relationship.trader.trader_score}\n"
        "Respond JSON: decision (COPY|SKIP), confidence (0-1), reasoning."
    )
    raw = client.complete(
        system="DreamLens copy advisor. Never override risk rules.",
        user=prompt,
        json_mode=True,
    )
    data = _parse_llm_object(
        raw, fallback={"decision": "SKIP", "confidence": 0.0, "reasoning": "Parse error"}
    )

    return {
        "decision": str(data.get("decision", "SKIP")).upper(),
        "confidence": float(data.get("confidence", 0)),
        "reasoning": data.get("reasoning", ""),
    }


@dataclass
class ParsedIntent:
    intent: str
    params: dict[str, Any]
    message: str = ""


def parse_intent(message: str) -> ParsedIntent:
    """Rule-based NL intent parsing — does not invent event IDs."""
    text = message.strip()
    lower = text.lower()

    trade_match = re.search(
        r"(?:buy|place|trade|put)\s+\$?([\d.]+)\s+(yes|no)\b(?:\s+on(?:\s+the)?\s+(btc|eth|bitcoin|ethereum)\b)?",
        lower,
    )
    if trade_match:
        asset_raw = (trade_match.group(3) or "").lower()
        params: dict[str, Any] = {
            "amount": Decimal(trade_match.group(1)),
            "outcome": trade_match.group(2).upper(),
        }
        if asset_raw in {"btc", "bitcoin"}:
            params["asset"] = "BTC"
        elif asset_raw in {"eth", "ethereum"}:
            params["asset"] = "ETH"
        return ParsedIntent(
            intent="PREPARE_TRADE",
            params=params,
            message="Ready to prepare trade — event must be selected separately.",
        )

    if re.search(r"\b(interesting|worth a look|radar|right now)\b", lower):
        return ParsedIntent(
            intent="SEARCH_EVENTS",
            params={"query": text, "sort": "interesting"},
            message="Showing events worth a look.",
        )

    if re.search(r"\b(top traders|traders buying|traders?\s+buy)\b", lower):
        return ParsedIntent(
            intent="SEARCH_EVENTS",
            params={"query": text, "sort": "interesting"},
            message="Showing events top traders are active in.",
        )

    if re.search(r"\b(ending soon|expir)\b", lower):
        return ParsedIntent(
            intent="SEARCH_EVENTS",
            params={"query": text, "sort": "expiry"},
            message="Showing events that end soon.",
        )

    if re.search(r"\b(btc|bitcoin|eth|ethereum|events?)\b", lower):
        asset = (
            "BTC"
            if "btc" in lower or "bitcoin" in lower
            else "ETH"
            if "eth" in lower or "ethereum" in lower
            else ""
        )
        return ParsedIntent(
            intent="SEARCH_EVENTS",
            params={"query": asset or text, "asset": asset},
            message=f"Searching events{f' for {asset}' if asset else ''}.",
        )

    if re.search(r"\b(consensus|traders?\s+think)\b", lower):
        return ParsedIntent(
            intent="GET_CONSENSUS",
            params={},
            message="Consensus requires an event context.",
        )

    if re.search(r"\b(analy[sz]e|analysis|estimate|probability)\b", lower):
        return ParsedIntent(
            intent="ANALYZE_EVENT",
            params={},
            message="Analysis requires an event context.",
        )

    if re.search(r"\b(portfolio|positions?|pnl)\b", lower):
        return ParsedIntent(
            intent="GET_PORTFOLIO",
            params={},
            message="Fetching portfolio summary.",
        )

    return ParsedIntent(
        intent="CHAT",
        params={},
        message=text,
    )


def chat(
    *,
    message: str,
    user=None,
    event_id: int | None = None,
) -> dict:
    """Process chat message — tools executed by backend, not LLM."""
    _check_rate_limit(user.pk if user else None)
    parsed = parse_intent(message)
    tool_results: dict[str, Any] = {}

    if parsed.intent == "SEARCH_EVENTS":
        from apps.events.models import EventContract

        qs = EventContract.objects.filter(
            status__in=[EventContract.Status.TRADING, EventContract.Status.LIVE]
        )
        asset = parsed.params.get("asset")
        if asset:
            qs = qs.filter(underlying_asset__iexact=asset)
        sort = parsed.params.get("sort")
        if sort == "expiry":
            qs = qs.order_by("expiry_time")
        else:
            qs = qs.order_by("-cumulative_quote_volume")
        events = []
        for row in qs[:10].values(
            "id", "title", "underlying_asset", "expiry_time", "cumulative_quote_volume"
        ):
            expiry = row["expiry_time"]
            events.append(
                {
                    "id": row["id"],
                    "title": row["title"],
                    "underlying_asset": row["underlying_asset"],
                    "expiry_time": expiry.isoformat() if expiry else None,
                    "cumulative_quote_volume": str(row["cumulative_quote_volume"]),
                }
            )
        tool_results["events"] = events
        tool_results["asset"] = asset
        tool_results["sort"] = sort
        reply = f"Found {len(events)} active events."
        if asset:
            reply = f"Found {len(events)} active {asset} events."
        elif sort == "expiry":
            reply = f"Found {len(events)} markets ending soon."
        elif sort == "interesting":
            reply = f"Here are {len(events)} markets with the most activity."

    elif parsed.intent == "ANALYZE_EVENT" and event_id:
        from apps.events.models import EventContract

        event = EventContract.objects.get(pk=event_id)
        analysis = analyze_event(event, user=user)
        tool_results["analysis"] = analysis
        reply = (
            f"DreamLens estimate: {analysis['estimated_probability']} vs market "
            f"{analysis['market_probability']} ({analysis['signal']}). {DISCLAIMER}"
        )

    elif parsed.intent == "GET_CONSENSUS" and event_id:
        from apps.events.models import EventContract
        from services.consensus_service import compute_consensus

        event = EventContract.objects.get(pk=event_id)
        consensus = compute_consensus(event)
        tool_results["consensus"] = consensus
        reply = (
            f"Trader consensus YES {consensus['yes_consensus']} / NO {consensus['no_consensus']} "
            f"({consensus['agreement_level']} agreement, {consensus['trader_count']} traders). "
            f"{consensus['disclaimer']}"
        )

    elif parsed.intent == "PREPARE_TRADE":
        tool_results["prepare_params"] = {
            "amount": str(parsed.params.get("amount", "10")),
            "outcome": parsed.params.get("outcome", "YES"),
        }
        # Surface a pick when no event context so the UI can open the trade modal.
        if event_id:
            from apps.events.models import EventContract

            event = EventContract.objects.filter(pk=event_id).values("id", "title").first()
            if event:
                tool_results["events"] = [event]
        else:
            from apps.events.models import EventContract

            row = (
                EventContract.objects.filter(
                    status__in=[EventContract.Status.TRADING, EventContract.Status.LIVE]
                )
                .order_by("-cumulative_quote_volume")
                .values("id", "title")
                .first()
            )
            if row:
                tool_results["events"] = [row]
        amount = tool_results["prepare_params"]["amount"]
        outcome = tool_results["prepare_params"]["outcome"]
        from services.event_copy import format_collateral

        reply = f"Ready to buy {format_collateral(amount, compact=True)} {outcome}. Confirm in the trade modal."

    elif parsed.intent == "GET_PORTFOLIO" and user:
        from services.portfolio_service import get_portfolio_summary

        summary = get_portfolio_summary(user)
        tool_results["portfolio"] = summary
        reply = f"Portfolio PnL {summary['total_pnl']}, {summary['open_positions']} open positions."

    else:
        client = get_llm_client()
        raw = client.complete(
            system=(
                "You are DreamLens assistant for DreamDEX event contracts. "
                "Never guarantee outcomes. Suggest searching events or analyzing markets."
            ),
            user=message,
        )
        data = _parse_llm_object(raw)
        reply = data.get("message") or raw if data else raw
        reply = reply if reply else "How can I help with DreamDEX events today?"

    return {
        "intent": parsed.intent,
        "reply": reply,
        "tool_results": tool_results,
        "disclaimer": DISCLAIMER,
    }


LENS_SYSTEM = (
    "You are Lens, a senior event-contract analyst for DreamLens covering live DreamDEX "
    "markets on Somnia Shannon. Write like a desk analyst: specific, numbered, and causal. "
    "Tie comments to the live book, strike, YES/NO prices, volume, time left, and supplied "
    "headlines. Do not invent quotes, prices, or news. If a headline is missing, say so. "
    "Commentary only — never guarantee outcomes, never construct transactions or private keys, "
    "and never instruct a specific buy or sell. Never describe DreamLens Score as a probability "
    "of winning. Always end with: " + DISCLAIMER
)

EXPLAIN_SYSTEM = (
    "You are Lens explaining one DreamDEX binary event in plain language. "
    "Respond as JSON with keys: setup, yes_needs, no_needs, in_the_price, could_change. "
    "Every value MUST be a single plain-English string of 2–4 sentences. "
    "Never nest objects, lists, or key/value maps inside those fields. "
    "Cite strike, YES/NO prices, volume, trades, and time left from the brief. "
    "setup: the question, strike, time left, and current YES/NO. "
    "yes_needs / no_needs: what must happen to the underlying by expiry. "
    "in_the_price: what traders already seem to be pricing, using YES/NO and volume. "
    "could_change: headlines and market risks that could flip this window. "
    "Do not suggest a trade. Do not invent headlines. Do not call Score a probability of winning."
)

EXPLAIN_SECTIONS = (
    ("setup", "What's going on"),
    ("yes_needs", "What YES needs"),
    ("no_needs", "What NO needs"),
    ("in_the_price", "What's already in the price"),
    ("could_change", "What could change this"),
)


def _live_market_book(limit: int = 12) -> list[dict[str, Any]]:
    from apps.events.models import EventContract, EventOutcome
    from django.utils import timezone

    from services.event_copy import as_cents, event_question

    now = timezone.now()
    qs = (
        EventContract.objects.filter(
            status__in=[EventContract.Status.TRADING, EventContract.Status.LIVE],
            expiry_time__gt=now,
        )
        .prefetch_related("outcomes")
        .order_by("-cumulative_quote_volume")[:limit]
    )
    book: list[dict[str, Any]] = []
    for event in qs:
        yes = next(
            (
                row
                for row in event.outcomes.all()
                if row.outcome_type == EventOutcome.OutcomeType.YES
            ),
            None,
        )
        mins = (event.expiry_time - now).total_seconds() / 60 if event.expiry_time else 0
        no = next(
            (
                row
                for row in event.outcomes.all()
                if row.outcome_type == EventOutcome.OutcomeType.NO
            ),
            None,
        )
        book.append(
            {
                "id": event.pk,
                "title": event_question(event),
                "underlying_asset": event.underlying_asset,
                "yes_price": str(yes.current_price) if yes and yes.current_price is not None else None,
                "yes_cents": as_cents(yes.current_price) if yes else None,
                "no_price": str(no.current_price) if no and no.current_price is not None else None,
                "no_cents": as_cents(no.current_price) if no else None,
                "volume": str(event.cumulative_quote_volume),
                "trade_count": int(event.trade_count or 0),
                "minutes_to_expiry": round(mins, 1),
            }
        )
    return book


def _format_market_book(book: list[dict[str, Any]]) -> str:
    if not book:
        return "No live DreamDEX events are trading right now."
    lines = []
    for row in book:
        lines.append(
            f"- [id={row['id']}] {row['title']} | {row['underlying_asset']} | "
            f"YES {row.get('yes_cents') or row.get('yes_price') or 'n/a'} | "
            f"NO {row.get('no_cents') or row.get('no_price') or 'n/a'} | "
            f"vol {row.get('volume')} | {row.get('trade_count', 0)} trades | "
            f"{row['minutes_to_expiry']}m left"
        )
    return "\n".join(lines)


def _events_mentioned(reply: str, book: list[dict[str, Any]]) -> list[dict[str, Any]]:
    text = (reply or "").lower()
    hits: list[dict[str, Any]] = []
    for row in book:
        title = (row.get("title") or "").lower()
        asset = (row.get("underlying_asset") or "").lower()
        token = f"id={row['id']}"
        if (title and title in text) or (asset and asset.lower() in text) or token in text:
            hits.append(
                {
                    "id": row["id"],
                    "title": row["title"],
                    "underlying_asset": row["underlying_asset"],
                }
            )
    return hits


def _as_prose(value: Any) -> str:
    """Turn nested LLM JSON into readable sentences."""
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, (int, float, Decimal)):
        return str(value)
    if isinstance(value, list):
        return " ".join(part for part in (_as_prose(item) for item in value) if part)
    if isinstance(value, dict):
        parts = []
        for key, item in value.items():
            text = _as_prose(item)
            if not text:
                continue
            label = str(key).replace("_", " ").strip()
            if label.lower() in {"text", "summary", "value", "detail"}:
                parts.append(text)
            else:
                parts.append(f"{label}: {text}")
        return " ".join(parts)
    return str(value).strip()


def _explanation_from_llm(data: dict[str, Any]) -> dict[str, str] | None:
    sections: dict[str, str] = {}
    for key, _label in EXPLAIN_SECTIONS:
        text = _as_prose(data.get(key))
        if text:
            sections[key] = text
    return sections or None


def _reply_from_explanation(sections: dict[str, str]) -> str:
    parts = []
    labels = dict(EXPLAIN_SECTIONS)
    for key, _label in EXPLAIN_SECTIONS:
        text = (sections.get(key) or "").strip()
        if text:
            parts.append(f"{labels[key]}\n{text}")
    return "\n\n".join(parts)


def lens_chat(
    *,
    message: str,
    history: list[dict[str, Any]] | None = None,
    event_id: int | None = None,
    structured: bool = False,
) -> dict[str, Any]:
    """Financial-analyst chat — no trade preparation."""
    _check_rate_limit(None)
    book = _live_market_book()
    focus = ""
    focused_event = None
    headlines: list[dict] = []
    if event_id:
        from services.event_copy import event_question

        focused = next((row for row in book if row.get("id") == event_id), None)
        focused_event = EventContract.objects.prefetch_related("outcomes", "radar_signals").filter(pk=event_id).first()
        if focused is None and focused_event is not None:
            focused = {
                "id": focused_event.pk,
                "title": event_question(focused_event),
                "underlying_asset": focused_event.underlying_asset,
            }
        if focused:
            headlines = _headlines_for_asset(focused.get("underlying_asset"))
            if focused_event is not None:
                focus = (
                    "Focus on this event only.\n"
                    f"{_event_analyst_brief(focused_event, headlines=headlines)}\n\n"
                )
            else:
                from services.market_news import format_headlines_for_prompt

                focus = (
                    f"Focus on this event only: {focused.get('title')} "
                    f"(id={focused.get('id')}, {focused.get('underlying_asset')}).\n"
                    f"Live headlines:\n{format_headlines_for_prompt(headlines)}\n\n"
                )
    else:
        from services.market_news import format_headlines_for_prompt

        headlines = _headlines_for_asset(None, limit=8)
        focus = f"Live headlines:\n{format_headlines_for_prompt(headlines)}\n\n"

    client = get_llm_client()
    explanation = None
    if structured and event_id:
        prompt = (
            f"{focus}"
            "Explain this market in the required JSON. What would have to happen for YES to win? "
            "Do not suggest a trade.\n\n"
            f"User question: {message.strip()}"
        )
        raw = client.complete(
            system=EXPLAIN_SYSTEM,
            user=prompt,
            history=history or [],
            json_mode=True,
            google_search=False,
            max_output_tokens=2048,
        )
        data = _parse_llm_object(raw)
        explanation = _explanation_from_llm(data)
        if data.get("available") is False:
            reasons = data.get("reasons") or ["AI analysis is unavailable."]
            reply = reasons[0] if isinstance(reasons, list) and reasons else str(reasons)
        elif explanation:
            reply = _reply_from_explanation(explanation)
        else:
            reply = data.get("message") or raw if data else raw
            reply = reply if reply else "I could not form a market view just now."
    else:
        prompt = (
            "Live DreamDEX book:\n"
            f"{_format_market_book(book)}\n\n"
            f"{focus}"
            "Write 3–6 short paragraphs. Cite prices, volume, time left, and headlines by name. "
            "If you mention a contract, use its question and id.\n\n"
            f"User question: {message.strip()}"
        )
        raw = client.complete(
            system=LENS_SYSTEM,
            user=prompt,
            history=history or [],
            google_search=True,
            max_output_tokens=3072,
        )
        data = _parse_llm_object(raw)
        if data.get("available") is False:
            reasons = data.get("reasons") or ["AI analysis is unavailable."]
            reply = reasons[0] if isinstance(reasons, list) and reasons else str(reasons)
        else:
            reply = data.get("message") or raw if data else raw
            reply = reply if reply else "I could not form a market view just now."
    if DISCLAIMER.lower() not in str(reply).lower():
        reply = f"{reply}\n\n{DISCLAIMER}"
    events = _events_mentioned(str(reply), book)
    return {
        "intent": "LENS",
        "reply": reply,
        "explanation": explanation,
        "tool_results": {"events": events, "book": book, "headlines": headlines[:8]},
        "disclaimer": DISCLAIMER,
    }


def search_events(query: str, *, limit: int = 20) -> list[dict]:
    from apps.events.models import EventContract
    from django.db.models import Q

    qs = EventContract.objects.filter(
        status__in=[EventContract.Status.TRADING, EventContract.Status.LIVE]
    )
    if query:
        qs = qs.filter(
            Q(title__icontains=query)
            | Q(underlying_asset__icontains=query)
            | Q(description__icontains=query)
        )
    return list(
        qs.order_by("-cumulative_quote_volume")[:limit].values(
            "id",
            "title",
            "underlying_asset",
            "status",
            "expiry_time",
            "cumulative_quote_volume",
        )
    )
