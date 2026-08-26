"""Long-poll Telegram updates when no public webhook URL is available."""

from __future__ import annotations

import time

from django.core.management.base import BaseCommand, CommandError

from integrations.telegram.client import (
    TelegramError,
    bot_configured,
    delete_webhook,
    get_updates,
)
from services.telegram_bot_service import handle_update


class Command(BaseCommand):
    help = "Poll Telegram getUpdates for the DreamAgent bot (local/dev)."

    def add_arguments(self, parser):
        parser.add_argument("--timeout", type=int, default=25)

    def handle(self, *args, **options):
        if not bot_configured():
            raise CommandError("TELEGRAM_BOT_TOKEN is not set")
        timeout = int(options["timeout"])
        offset = None
        try:
            delete_webhook(drop_pending=False)
        except TelegramError as exc:
            self.stderr.write(f"Could not clear webhook (getUpdates may conflict): {exc}")
        self.stdout.write("Polling Telegram for DreamLens bot updates…")
        while True:
            try:
                updates = get_updates(offset=offset, timeout=timeout)
            except TelegramError as exc:
                self.stderr.write(str(exc))
                time.sleep(3)
                continue
            except KeyboardInterrupt:
                self.stdout.write("Stopped.")
                return
            for update in updates:
                uid = int(update.get("update_id") or 0)
                offset = uid + 1
                handle_update(update)
