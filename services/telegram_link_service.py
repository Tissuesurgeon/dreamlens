"""Link a Telegram chat to a DreamLens wallet user."""

from __future__ import annotations

import logging
import secrets
from datetime import timedelta

from django.conf import settings
from django.db import connection, transaction
from django.db.utils import ProgrammingError
from django.utils import timezone

from apps.accounts.models import TelegramLink, Wallet
from integrations.telegram.client import (
    TelegramError,
    bot_url,
    bot_username,
    html_escape,
    inline_keyboard,
    send_html,
)

logger = logging.getLogger("dreamlens.telegram.link")

LINK_TTL = timedelta(minutes=15)


class TelegramLinkError(Exception):
    pass


def serialize_link(link: TelegramLink | None) -> dict:
    username = bot_username()
    payload = {
        "chat_id": None,
        "status": None,
        "bot_username": username,
        "bot_url": bot_url(),
        "linked_at": None,
        "start_param": "chatid",
    }
    if link is None:
        return payload
    start_param = "chatid"
    if link.status == TelegramLink.Status.PENDING and link.confirm_token:
        start_param = f"ok_{link.confirm_token}"
    payload.update(
        {
            "chat_id": str(link.chat_id),
            "status": link.status,
            "linked_at": link.linked_at.isoformat() if link.linked_at else None,
            "start_param": start_param,
        }
    )
    return payload


def get_link(user) -> TelegramLink | None:
    if not getattr(user, "is_authenticated", False):
        return None
    try:
        return TelegramLink.objects.filter(user=user).first()
    except ProgrammingError:
        logger.warning(
            "TelegramLink table missing — apply accounts migrations",
            exc_info=True,
        )
        try:
            connection.rollback()
        except Exception:
            pass
        return None


def active_link_for_chat(chat_id: int) -> TelegramLink | None:
    return (
        TelegramLink.objects.filter(chat_id=int(chat_id), status=TelegramLink.Status.ACTIVE)
        .select_related("user")
        .first()
    )


def _primary_wallet_label(user) -> str:
    wallet = (
        Wallet.objects.filter(user=user, is_primary=True).first()
        or Wallet.objects.filter(user=user).first()
    )
    if not wallet:
        return user.username
    addr = wallet.address
    if len(addr) > 12:
        return f"{addr[:6]}…{addr[-4:]}"
    return addr


@transaction.atomic
def start_link(user, chat_id: int) -> TelegramLink:
    try:
        chat_id = int(chat_id)
    except (TypeError, ValueError) as exc:
        raise TelegramLinkError("Chat ID must be a number.") from exc
    if chat_id == 0:
        raise TelegramLinkError("Invalid chat ID.")

    other = (
        TelegramLink.objects.select_for_update()
        .filter(chat_id=chat_id)
        .exclude(user=user)
        .first()
    )
    if other and other.status == TelegramLink.Status.ACTIVE:
        raise TelegramLinkError("This Telegram chat is already linked to another wallet.")
    if other:
        other.delete()

    existing = get_link(user)
    if existing and existing.status == TelegramLink.Status.ACTIVE:
        raise TelegramLinkError("Unlink Telegram first.")

    token = secrets.token_urlsafe(12)
    expires = timezone.now() + LINK_TTL
    link, _created = TelegramLink.objects.update_or_create(
        user=user,
        defaults={
            "chat_id": chat_id,
            "status": TelegramLink.Status.PENDING,
            "confirm_token": token,
            "confirm_expires_at": expires,
            "linked_at": None,
        },
    )

    label = _primary_wallet_label(user)
    try:
        send_html(
            chat_id,
            f"Link this DreamLens wallet (<code>{html_escape(label)}</code>) to this Telegram chat?\n"
            "Confirm only if you started this from Portfolio.",
            reply_markup=inline_keyboard(
                [
                    [
                        ("Confirm", f"tg:ok:{token}"),
                        ("Cancel", f"tg:no:{token}"),
                    ]
                ]
            ),
        )
    except TelegramError as exc:
        raise TelegramLinkError(
            "Could not message that chat. Open the bot, tap Start, then paste the chat ID again."
        ) from exc
    return link


@transaction.atomic
def confirm_link(*, chat_id: int, token: str) -> TelegramLink:
    link = (
        TelegramLink.objects.select_for_update()
        .filter(
            chat_id=int(chat_id),
            confirm_token=token,
            status=TelegramLink.Status.PENDING,
        )
        .select_related("user")
        .first()
    )
    if not link:
        raise TelegramLinkError("This link request expired. Paste your chat ID on Portfolio again.")
    if link.confirm_expires_at and link.confirm_expires_at < timezone.now():
        raise TelegramLinkError("This link request expired. Paste your chat ID on Portfolio again.")
    clash = (
        TelegramLink.objects.filter(chat_id=link.chat_id, status=TelegramLink.Status.ACTIVE)
        .exclude(pk=link.pk)
        .exists()
    )
    if clash:
        raise TelegramLinkError("This Telegram chat is already linked to another wallet.")
    link.status = TelegramLink.Status.ACTIVE
    link.linked_at = timezone.now()
    link.confirm_token = ""
    link.confirm_expires_at = None
    link.save(
        update_fields=["status", "linked_at", "confirm_token", "confirm_expires_at", "updated_at"]
    )
    return link


@transaction.atomic
def cancel_pending_link(*, chat_id: int, token: str) -> None:
    TelegramLink.objects.filter(
        chat_id=int(chat_id),
        confirm_token=token,
        status=TelegramLink.Status.PENDING,
    ).delete()


@transaction.atomic
def unlink(user) -> None:
    TelegramLink.objects.filter(user=user).delete()


def site_origin() -> str:
    import os

    explicit = (
        getattr(settings, "DREAMLENS_PUBLIC_URL", "")
        or os.environ.get("DREAMLENS_PUBLIC_URL")
        or ""
    ).strip().rstrip("/")
    if explicit:
        return explicit
    railway = (os.environ.get("RAILWAY_PUBLIC_DOMAIN") or "").strip()
    if railway:
        host = railway.split("//")[-1].split("/")[0]
        if host:
            return f"https://{host}"
    origins = getattr(settings, "CSRF_TRUSTED_ORIGINS", None) or []
    for origin in origins:
        origin = str(origin).rstrip("/")
        if origin and "*" not in origin:
            return origin
    return "http://127.0.0.1:8000"
