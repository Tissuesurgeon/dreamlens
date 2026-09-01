"""Enable RLS and revoke Data API grants on public tables."""

from __future__ import annotations

from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import connection


SQL_RELATIVE = Path("scripts/sql/lock_public_data_api.sql")


def split_sql_statements(script: str) -> list[str]:
    """Split a SQL script on semicolons, keeping dollar-quoted bodies intact."""
    statements: list[str] = []
    buf: list[str] = []
    i = 0
    in_dollar = False
    while i < len(script):
        if script.startswith("$$", i):
            in_dollar = not in_dollar
            buf.append("$$")
            i += 2
            continue
        ch = script[i]
        if ch == ";" and not in_dollar:
            stmt = "".join(buf).strip()
            if stmt and not _comments_only(stmt):
                statements.append(stmt)
            buf = []
            i += 1
            continue
        buf.append(ch)
        i += 1
    tail = "".join(buf).strip()
    if tail and not _comments_only(tail):
        statements.append(tail)
    return statements


def _comments_only(stmt: str) -> bool:
    lines = [line.strip() for line in stmt.splitlines() if line.strip()]
    return all(line.startswith("--") for line in lines)


class Command(BaseCommand):
    help = (
        "Enable Row Level Security on every public table and revoke anon/"
        "authenticated grants so the Supabase Data API cannot read DreamLens data."
    )

    def handle(self, *args, **options):
        if connection.vendor != "postgresql":
            raise CommandError(
                f"Requires PostgreSQL (Supabase). Refusing to run on {connection.vendor}."
            )

        sql_path = Path(settings.BASE_DIR) / SQL_RELATIVE
        if not sql_path.is_file():
            raise CommandError(f"Missing SQL script: {sql_path}")

        sql = sql_path.read_text(encoding="utf-8")
        statements = split_sql_statements(sql)
        if not statements:
            raise CommandError(f"No SQL statements in {sql_path}")

        with connection.cursor() as cursor:
            for statement in statements:
                cursor.execute(statement)
            cursor.execute(
                """
                SELECT c.relname
                FROM pg_class c
                JOIN pg_namespace nsp ON nsp.oid = c.relnamespace
                WHERE nsp.nspname = 'public'
                  AND c.relkind IN ('r', 'p')
                  AND NOT c.relrowsecurity
                ORDER BY 1
                """
            )
            unlocked = [row[0] for row in cursor.fetchall()]
            cursor.execute(
                """
                SELECT table_name
                FROM information_schema.role_table_grants
                WHERE table_schema = 'public'
                  AND grantee IN ('anon', 'authenticated')
                GROUP BY table_name
                ORDER BY 1
                """
            )
            still_granted = [row[0] for row in cursor.fetchall()]
            cursor.execute(
                """
                SELECT count(*)
                FROM pg_class c
                JOIN pg_namespace nsp ON nsp.oid = c.relnamespace
                WHERE nsp.nspname = 'public'
                  AND c.relkind IN ('r', 'p')
                  AND c.relrowsecurity
                  AND NOT c.relforcerowsecurity
                """
            )
            locked = cursor.fetchone()[0]

        if unlocked:
            raise CommandError(
                "RLS still disabled on: " + ", ".join(unlocked)
            )
        if still_granted:
            raise CommandError(
                "anon/authenticated still granted on: " + ", ".join(still_granted)
            )

        self.stdout.write(
            self.style.SUCCESS(
                f"Locked {locked} public tables (RLS on, no FORCE, Data API grants revoked)."
            )
        )
        self.stdout.write(
            "Re-run this command after any Django migration that creates public tables."
        )
