from unittest.mock import MagicMock, patch

import pytest
from django.test import override_settings

from integrations.supabase.client import (
    SupabaseConfigError,
    configured,
    get_client,
    ping,
    reset_client,
)


@pytest.fixture(autouse=True)
def _reset_supabase_client():
    reset_client()
    yield
    reset_client()


@override_settings(SUPABASE_URL="", SUPABASE_KEY="")
def test_not_configured_without_env():
    assert configured() is False
    with pytest.raises(SupabaseConfigError):
        get_client()


@override_settings(
    SUPABASE_URL="https://example.supabase.co",
    SUPABASE_KEY="sb_publishable_test",
)
def test_ping_returns_todos_when_table_exists():
    fake = MagicMock()
    fake.table.return_value.select.return_value.limit.return_value.execute.return_value.data = [
        {"name": "write tests"}
    ]
    health = MagicMock()
    health.content = b"{}"
    health.json.return_value = {"name": "GoTrue"}
    health.raise_for_status.return_value = None
    with (
        patch("integrations.supabase.client.create_client", return_value=fake),
        patch("integrations.supabase.client.httpx.get", return_value=health),
    ):
        result = ping()
    assert result["todos"] == [{"name": "write tests"}]
    assert result["todos_error"] is None
    fake.table.assert_called_once_with("todos")


@override_settings(
    SUPABASE_URL="https://example.supabase.co",
    SUPABASE_KEY="sb_publishable_test",
)
def test_ping_records_missing_todos_table():
    from postgrest.exceptions import APIError

    fake = MagicMock()
    fake.table.return_value.select.return_value.limit.return_value.execute.side_effect = APIError(
        {"message": "Could not find the table 'public.todos'", "code": "PGRST205"}
    )
    health = MagicMock()
    health.content = b"{}"
    health.json.return_value = {"name": "GoTrue"}
    health.raise_for_status.return_value = None
    with (
        patch("integrations.supabase.client.create_client", return_value=fake),
        patch("integrations.supabase.client.httpx.get", return_value=health),
    ):
        result = ping()
    assert result["todos"] == []
    assert "todos" in (result["todos_error"] or "")
