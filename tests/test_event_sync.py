"""Event sync from DreamDEX adapter."""

from __future__ import annotations

import time
from datetime import timedelta
from decimal import Decimal

import pytest
from django.core.cache import cache
from django.db import IntegrityError
from django.utils import timezone

from apps.events.models import EventContract, EventOutcome
from services.event_service import refresh_events_from_dreamdex, sync_events


@pytest.mark.django_db
def test_sync_events_upserts_from_mock_adapter(mock_adapter):
    first = sync_events()
    assert first["total"] > 0
    assert EventContract.objects.count() == first["total"]
    assert EventOutcome.objects.count() == EventContract.objects.count() * 2

    second = sync_events()
    assert second["created"] == 0
    assert second["updated"] == first["total"]


@pytest.mark.django_db
def test_sync_events_marks_stale_markets_locked(mock_adapter):
    sync_events()
    orphan = EventContract.objects.create(
        external_id="0xorphan0000000000000000000000000000000001",
        title="Orphan market",
        underlying_asset="BTC",
        status=EventContract.Status.TRADING,
        expiry_time=timezone.now() + timedelta(hours=1),
        yes_identifier="yes-orphan",
        no_identifier="no-orphan",
        cumulative_quote_volume=Decimal("10"),
    )
    result = sync_events()
    orphan.refresh_from_db()
    assert orphan.status == EventContract.Status.LOCKED
    assert result["stale"] >= 1


@pytest.mark.django_db
def test_sync_events_fetches_winner_for_dropped_live_market(sample_event, mock_adapter):
    mock_adapter.simulate_settlement(sample_event.external_id, "NO")
    result = sync_events()
    sample_event.refresh_from_db()
    assert sample_event.status == EventContract.Status.RESOLVED
    assert sample_event.winning_outcome == "NO"
    assert result["stale"] == 0


@pytest.mark.django_db
def test_refresh_events_from_dreamdex_uses_cache(mock_adapter, settings):
    cache.clear()
    settings.DREAMDEX_EVENT_SYNC_INTERVAL = 60
    first = refresh_events_from_dreamdex(force=True)
    assert first is not None and first["total"] > 0
    second = refresh_events_from_dreamdex()
    assert second is None  # still fresh within TTL


@pytest.mark.django_db
def test_refresh_events_ignores_cache_when_local_index_is_empty(mock_adapter, settings):
    cache.clear()
    settings.DREAMDEX_EVENT_SYNC_INTERVAL = 60
    cache.set("dreamdex:events:fresh", "stale-flag", timeout=60)
    assert EventContract.objects.count() == 0
    result = refresh_events_from_dreamdex()
    assert result is not None and result["total"] > 0
    assert EventContract.objects.exists()


@pytest.mark.django_db
def test_refresh_does_not_block_when_local_markets_exist(mock_adapter, settings, monkeypatch):
    cache.clear()
    sync_events()
    cache.clear()
    settings.DREAMDEX_EVENT_SYNC_INTERVAL = 60

    def slow_sync(**kwargs):
        time.sleep(1.2)
        return {"created": 0, "updated": 0, "stale": 0, "total": 0}

    monkeypatch.setattr("services.event_service.sync_events", slow_sync)
    started = time.perf_counter()
    result = refresh_events_from_dreamdex()
    elapsed = time.perf_counter() - started
    assert result is None
    assert elapsed < 0.4


@pytest.mark.django_db
def test_external_id_unique(mock_adapter):
    sync_events()
    existing = EventContract.objects.first()
    assert existing is not None

    with pytest.raises(IntegrityError):
        EventContract.objects.create(
            external_id=existing.external_id,
            title="Duplicate external id",
            underlying_asset="BTC",
            expiry_time=existing.expiry_time,
            yes_identifier="dup-yes",
            no_identifier="dup-no",
        )
