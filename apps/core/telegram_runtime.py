"""Receive Telegram updates without a separate worker.

Webhook needs TELEGRAM_WEBHOOK_SECRET and a public HTTPS origin.
Otherwise one process long-polls getUpdates so /start and Confirm actually arrive.
"""

from __future__ import annotations

import logging
import os
import sys
import tempfile
import threading
import time

logger = logging.getLogger("dreamlens.telegram.runtime")

_LOCK_FH = None
_POLL_THREAD = None
_SKIP_COMMANDS = {
    "migrate",
    "makemigrations",
    "collectstatic",
    "shell",
    "dbshell",
    "createsuperuser",
    "test",
    "flush",
    "check",
    "dumpdata",
    "loaddata",
}


def _django_command() -> str:
    args = [str(a) for a in sys.argv[1:] if not str(a).startswith("-")]
    return args[0] if args else ""


def should_listen() -> bool:
    if os.environ.get("TELEGRAM_DISABLE_POLL", "").lower() in {"1", "true", "yes"}:
        return False
    if os.environ.get("PYTEST_CURRENT_TEST"):
        return False
    if "pytest" in sys.modules or os.path.basename(sys.argv[0]).startswith("pytest"):
        return False
    command = _django_command()
    if command in _SKIP_COMMANDS:
        return False
    if command == "runserver" and os.environ.get("RUN_MAIN") != "true":
        return False
    return True


def _acquire_poll_lock() -> bool:
    global _LOCK_FH
    path = os.path.join(tempfile.gettempdir(), "dreamlens-telegram-poll.lock")
    handle = open(path, "w")
    try:
        import fcntl

        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        return False
    _LOCK_FH = handle
    return True


def run_poll_loop(*, timeout: int = 25) -> None:
    from django.db import connections

    from integrations.telegram.client import TelegramError, delete_webhook, get_updates
    from services.telegram_bot_service import handle_update

    try:
        delete_webhook(drop_pending=False)
    except TelegramError as exc:
        logger.warning("could not clear telegram webhook: %s", exc)
    logger.info("polling Telegram getUpdates")
    offset = None
    while True:
        # Drop the slot before the long HTTP wait so session-mode poolers
        # (pool_size 15) are not occupied for 25s of getUpdates idle time.
        connections.close_all()
        try:
            updates = get_updates(offset=offset, timeout=timeout)
        except TelegramError as exc:
            logger.warning("telegram getUpdates failed: %s", exc)
            time.sleep(3)
            continue
        for update in updates:
            uid = int(update.get("update_id") or 0)
            offset = uid + 1
            try:
                handle_update(update)
            finally:
                connections.close_all()


def start_telegram_listener() -> None:
    if not should_listen():
        return
    from django.conf import settings
    from integrations.telegram.client import TelegramError, bot_configured, set_webhook
    from services.telegram_link_service import site_origin

    if not bot_configured():
        return

    secret = (getattr(settings, "TELEGRAM_WEBHOOK_SECRET", "") or "").strip()
    origin = site_origin()
    webhook_url = f"{origin}/api/telegram/webhook/"
    if secret and origin.startswith("https://") and "*" not in origin:
        try:
            set_webhook(webhook_url, secret)
            logger.info("telegram webhook registered")
            return
        except TelegramError as exc:
            logger.warning("telegram setWebhook failed; falling back to poll: %s", exc)

    global _POLL_THREAD
    if _POLL_THREAD and _POLL_THREAD.is_alive():
        return
    if not _acquire_poll_lock():
        logger.info("telegram poller already running in another process")
        return
    _POLL_THREAD = threading.Thread(
        target=run_poll_loop,
        name="dreamlens-telegram-poll",
        daemon=True,
    )
    _POLL_THREAD.start()
