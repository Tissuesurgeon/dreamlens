"""Telegram Bot API client (httpx, no async event loop)."""

from __future__ import annotations

import logging
from typing import Any

import httpx
from django.conf import settings

logger = logging.getLogger("dreamlens.telegram")
# Bot tokens live in the request path; keep httpx from logging them.
logging.getLogger("httpx").setLevel(logging.WARNING)

API_ROOT = "https://api.telegram.org"


class TelegramError(Exception):
    pass


def bot_configured() -> bool:
    return bool((getattr(settings, "TELEGRAM_BOT_TOKEN", "") or "").strip())


def bot_username() -> str:
    return (getattr(settings, "TELEGRAM_BOT_USERNAME", "") or "").strip().lstrip("@")


def bot_url() -> str:
    name = bot_username()
    return f"https://t.me/{name}" if name else ""


def explorer_tx_url(tx_hash: str) -> str:
    network = (getattr(settings, "DREAMDEX_NETWORK", "testnet") or "testnet").lower()
    base = (
        "https://explorer.somnia.network"
        if network == "mainnet"
        else "https://shannon-explorer.somnia.network"
    )
    if not tx_hash:
        return base
    return f"{base}/tx/{tx_hash}"


def _token() -> str:
    token = (getattr(settings, "TELEGRAM_BOT_TOKEN", "") or "").strip()
    if not token:
        raise TelegramError("TELEGRAM_BOT_TOKEN is not set")
    return token


def _redact(text: str) -> str:
    token = (getattr(settings, "TELEGRAM_BOT_TOKEN", "") or "").strip()
    if token and token in text:
        return text.replace(token, "***")
    return text


def _post(method: str, payload: dict[str, Any]) -> dict[str, Any]:
    url = f"{API_ROOT}/bot{_token()}/{method}"
    try:
        response = httpx.post(url, json=payload, timeout=35.0)
        response.raise_for_status()
        data = response.json()
    except Exception as exc:  # noqa: BLE001
        logger.warning("telegram %s failed: %s", method, _redact(str(exc)))
        raise TelegramError(_redact(str(exc))) from exc
    if not data.get("ok"):
        raise TelegramError(data.get("description") or f"{method} failed")
    return data.get("result") or {}


def send_message(
    chat_id: int,
    text: str,
    *,
    reply_markup: dict[str, Any] | None = None,
    parse_mode: str | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "chat_id": int(chat_id),
        "text": text,
        "disable_web_page_preview": True,
    }
    if reply_markup:
        payload["reply_markup"] = reply_markup
    if parse_mode:
        payload["parse_mode"] = parse_mode
    return _post("sendMessage", payload)


def answer_callback(callback_id: str, text: str = "") -> None:
    payload: dict[str, Any] = {"callback_query_id": callback_id}
    if text:
        payload["text"] = text[:180]
    try:
        _post("answerCallbackQuery", payload)
    except TelegramError:
        logger.warning("answerCallbackQuery failed id=%s", callback_id)


def inline_keyboard(rows: list[list[tuple[str, str]]]) -> dict[str, Any]:
    """rows of (label, callback_data)."""
    return {
        "inline_keyboard": [
            [{"text": label, "callback_data": data} for label, data in row]
            for row in rows
        ]
    }


def delete_webhook(*, drop_pending: bool = False) -> None:
    """Clear a Bot API webhook so getUpdates can receive Confirm taps."""
    _post("deleteWebhook", {"drop_pending_updates": bool(drop_pending)})


def get_updates(*, offset: int | None = None, timeout: int = 25) -> list[dict[str, Any]]:
    payload: dict[str, Any] = {"timeout": int(timeout), "allowed_updates": ["message", "callback_query"]}
    if offset is not None:
        payload["offset"] = int(offset)
    url = f"{API_ROOT}/bot{_token()}/getUpdates"
    response = httpx.post(url, json=payload, timeout=timeout + 10)
    response.raise_for_status()
    data = response.json()
    if not data.get("ok"):
        raise TelegramError(data.get("description") or "getUpdates failed")
    return list(data.get("result") or [])
