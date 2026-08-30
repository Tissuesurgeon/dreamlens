"""Long-poll Telegram updates when no public webhook URL is available."""

from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError

from integrations.telegram.client import bot_configured


class Command(BaseCommand):
    help = "Poll Telegram getUpdates for the DreamLens bot (local/dev)."

    def add_arguments(self, parser):
        parser.add_argument("--timeout", type=int, default=25)

    def handle(self, *args, **options):
        if not bot_configured():
            raise CommandError("TELEGRAM_BOT_TOKEN is not set")
        from apps.core.telegram_runtime import run_poll_loop

        self.stdout.write("Polling Telegram for DreamLens bot updates…")
        try:
            run_poll_loop(timeout=int(options["timeout"]))
        except KeyboardInterrupt:
            self.stdout.write("Stopped.")
