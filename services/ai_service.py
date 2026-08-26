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


def _as_str_list(value: Any, fallback: str) -> list[str]:
    if isinstance(value, str) and value.strip():
        return [value.strip()]
    if isinstance(value, list):
        items = []
        for item in value:
            if isinstance(item, str) and item.strip():
                items.append(item.strip())
            elif isinstance(item, dict):
                text = item.get("text") or item.get("reason") or item.get("message")
                if text:
                    items.append(str(text))
        if items:
            return items
    return [fallback]

_rate_limit: dict[str, float] = {}
RATE_LIMIT_SECONDS = 1.0


@runtime_checkable
class LLMClient(Protocol):
    def complete(self, *, system: str, user: str, json_mode: bool = False) -> str: ...


class MockLLMClient:
    """Deterministic mock responses when no API key is configured."""

    def complete(self, *, system: str, user: str, json_mode: bool = False) -> str:
        if "analyze" in user.lower() or "event" in system.lower():
            return json.dumps(
                {
                    "estimated_probability": 0.58,
                    "market_probability": 0.52,
                    "confidence": 0.62,
                    "signal": "LEAN_YES",
                    "reasons": [
                        "Price momentum slightly bullish",
                        "Volume above recent average",
                    ],
                    "risks": [
                        "Short time to expiry increases variance",
                        "Consensus not unanimous among traders",
                    ],
                    "label": "DreamLens estimate",
                    "disclaimer": DISCLAIMER,
                }
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
        base_url: str = "https://api.openai.com/v1",
        label: str = "primary",
        timeout: float = 30,
    ) -> None:
        self.api_key = api_key
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.label = label
        self.timeout = timeout

    def complete(self, *, system: str, user: str, json_mode: bool = False) -> str:
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
            if use_json_mode:
                payload["response_format"] = {"type": "json_object"}

            req = urllib.request.Request(
                f"{self.base_url}/chat/completions",
                data=json.dumps(payload).encode(),
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                },
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                body = json.loads(resp.read().decode())
            choices = body.get("choices", [])
            if not choices:
                return "{}"
            return choices[0].get("message", {}).get("content", "{}")

        try:
            return _request(json_mode)
        except urllib.error.HTTPError as exc:
            # Many local servers reject response_format — retry without it.
            if json_mode and exc.code in (400, 422):
                logger.info("%s LLM rejected json_mode; retrying plain", self.label)
                try:
                    return _request(False)
                except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as retry_exc:
                    raise RuntimeError(f"{self.label} LLM failed: {retry_exc}") from retry_exc
            raise RuntimeError(f"{self.label} LLM HTTP {exc.code}: {exc.reason}") from exc
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"{self.label} LLM failed: {exc}") from exc


class UnavailableLLMClient:
    """Explicit failure — never invent market probabilities."""

    def complete(self, *, system: str, user: str, json_mode: bool = False) -> str:
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

    def complete(self, *, system: str, user: str, json_mode: bool = False) -> str:
        for client in self.clients:
            label = getattr(client, "label", client.__class__.__name__)
            try:
                return client.complete(system=system, user=user, json_mode=json_mode)
            except Exception as exc:  # noqa: BLE001 — cascade to next provider
                logger.warning("LLM provider %s failed: %s", label, exc)
        if getattr(settings, "MOCK_DREAMDEX", False):
            return MockLLMClient().complete(system=system, user=user, json_mode=json_mode)
        return UnavailableLLMClient().complete(system=system, user=user, json_mode=json_mode)


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


def get_llm_client() -> LLMClient:
    """Primary cloud LLM → local Ollama/LM Studio → mock."""
    chain: list[LLMClient] = []

    api_key = settings.LLM_API_KEY
    if api_key:
        chain.append(
            OpenAICompatibleClient(
                api_key=api_key,
                model=settings.LLM_MODEL or "gpt-4o-mini",
                base_url=getattr(settings, "LLM_BASE_URL", None) or "https://api.openai.com/v1",
                label=settings.LLM_PROVIDER or "primary",
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


def analyze_event(event: EventContract, *, user=None) -> dict:
    """Return structured DreamLens estimate for an event."""
    cache_key = (
        f"ai:analyze:{event.pk}:{event.last_price}:{event.cumulative_quote_volume}"
    )
    if user is None:
        cached = cache.get(cache_key)
        if cached is not None:
            return cached

    _check_rate_limit(user.pk if user else None)

    yes = event.outcomes.filter(outcome_type=EventOutcome.OutcomeType.YES).first()
    market_prob = float(yes.current_price) if yes else 0.5

    client = get_llm_client()
    prompt = (
        f"Analyze binary event: {event.title}\n"
        f"Asset: {event.underlying_asset}\n"
        f"YES price: {market_prob}\n"
        f"Volume: {event.cumulative_quote_volume}\n"
        f"Expiry minutes: {event.minutes_to_expiry:.1f}\n"
        "Respond with JSON keys: estimated_probability, market_probability, "
        "confidence, signal, reasons, risks."
    )
    raw = client.complete(
        system="You are DreamLens AI. Provide estimates only — never guarantees.",
        user=prompt,
        json_mode=True,
    )
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        data = {}

    result = {
        "event_id": event.pk,
        "title": event.title,
        "estimated_probability": Decimal(str(data.get("estimated_probability", market_prob + 0.03))).quantize(
            FOUR_PLACES
        ),
        "market_probability": Decimal(str(data.get("market_probability", market_prob))).quantize(FOUR_PLACES),
        "confidence": Decimal(str(data.get("confidence", 0.55))).quantize(FOUR_PLACES),
        "signal": data.get("signal", "NEUTRAL"),
        "reasons": _as_str_list(data.get("reasons"), "Insufficient LLM detail — using market baseline"),
        "risks": _as_str_list(data.get("risks"), "Market can move quickly near expiry"),
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
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        data = {"decision": "SKIP", "confidence": 0.0, "reasoning": "Parse error"}

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
        reply = f"Ready to buy ${amount} {outcome}. Confirm in the trade modal."

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
        try:
            data = json.loads(raw)
            reply = data.get("message", raw)
        except json.JSONDecodeError:
            reply = raw if raw else "How can I help with DreamDEX events today?"

    return {
        "intent": parsed.intent,
        "reply": reply,
        "tool_results": tool_results,
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
