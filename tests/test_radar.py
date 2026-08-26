"""Event Radar scoring and signal generation."""

from __future__ import annotations

from decimal import Decimal

import pytest

from apps.events.models import EventRadarSignal
from services.radar_service import compute_interest_score, generate_radar_signals


@pytest.mark.parametrize(
    "yes_price,momentum,volume,liquidity,minutes_to_expiry",
    [
        (Decimal("0.50"), Decimal("0"), Decimal("0"), Decimal("0"), 30.0),
        (Decimal("0.72"), Decimal("0.03"), Decimal("120000"), Decimal("50"), 10.0),
        (Decimal("0.28"), Decimal("-0.02"), Decimal("90000"), Decimal("30"), 5.0),
        (Decimal("0.55"), Decimal("0.01"), Decimal("200000"), Decimal("80"), 0.0),
    ],
)
def test_interest_score_normalized_0_100(
    yes_price,
    momentum,
    volume,
    liquidity,
    minutes_to_expiry,
):
    score = compute_interest_score(
        yes_price=yes_price,
        momentum=momentum,
        volume=volume,
        liquidity=liquidity,
        minutes_to_expiry=minutes_to_expiry,
    )
    assert Decimal("0") <= score <= Decimal("1")
    scaled = int(score * 100)
    assert 0 <= scaled <= 100


@pytest.mark.django_db
def test_generate_radar_signals_creates_rows(sample_event, mock_adapter):
    sample_event.cumulative_quote_volume = Decimal("100000")
    sample_event.save(update_fields=["cumulative_quote_volume"])

    result = generate_radar_signals()

    assert result["created"] >= 1
    assert EventRadarSignal.objects.filter(event=sample_event, is_active=True).exists()
