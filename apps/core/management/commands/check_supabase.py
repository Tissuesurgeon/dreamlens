"""Verify the Supabase Data API (HTTPS) client."""

from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError

from integrations.supabase.client import SupabaseConfigError, ping


class Command(BaseCommand):
    help = "Ping the Supabase Data API with SUPABASE_URL and SUPABASE_KEY."

    def handle(self, *args, **options):
        try:
            result = ping()
        except SupabaseConfigError as exc:
            raise CommandError(str(exc)) from exc
        except Exception as exc:
            raise CommandError(f"Supabase Data API failed: {exc}") from exc

        self.stdout.write(self.style.SUCCESS(f"Connected: {result['url']}"))
        self.stdout.write(f"  Auth health: {result['auth_health']}")
        if result["todos_error"]:
            self.stdout.write(
                self.style.WARNING(f"  todos table: {result['todos_error']}")
            )
        else:
            self.stdout.write(
                self.style.SUCCESS(f"  todos rows: {len(result['todos'])}")
            )
