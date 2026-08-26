"""Risk engine tests — deterministic gates AI cannot override."""

from __future__ import annotations

from decimal import Decimal

import pytest

from apps.dreamcopy.models import CopyRelationship
from apps.events.models import EventOutcome
from services.risk_service import RiskContext, RiskEngine


@pytest.mark.django_db
def test_expired_event_cannot_be_traded(expired_event, wallet):
    yes = expired_event.outcomes.get(outcome_type=EventOutcome.OutcomeType.YES)
    ctx = RiskContext(
        event=expired_event,
        outcome=yes,
        amount=Decimal("10"),
        wallet_address=wallet.address,
    )
    ok, reasons = RiskEngine().reject(ctx)
    assert ok is False
    assert any("expired" in r.lower() for r in reasons)


@pytest.mark.django_db
def test_invalid_outcome_rejected(sample_event, wallet, db):
    other = sample_event.__class__.objects.create(
        external_id="0xother00000000000000000000000000000001",
        title="Other event",
        underlying_asset="ETH",
        status=sample_event.status,
        expiry_time=sample_event.expiry_time,
        yes_identifier="yes-other",
        no_identifier="no-other",
    )
    foreign_outcome = EventOutcome.objects.create(
        event=other,
        outcome_type=EventOutcome.OutcomeType.YES,
        external_identifier="yes-other",
        current_price=Decimal("0.50"),
    )
    yes = sample_event.outcomes.get(outcome_type=EventOutcome.OutcomeType.YES)
    ctx = RiskContext(
        event=sample_event,
        outcome=foreign_outcome,
        amount=Decimal("10"),
        wallet_address=wallet.address,
    )
    ok, reasons = RiskEngine().reject(ctx)
    assert ok is False
    assert any("does not belong" in r for r in reasons)


@pytest.mark.django_db
def test_amount_above_max_per_trade_rejected(sample_event, wallet, copy_relationship):
    yes = sample_event.outcomes.get(outcome_type=EventOutcome.OutcomeType.YES)
    ctx = RiskContext(
        event=sample_event,
        outcome=yes,
        amount=Decimal("100"),
        wallet_address=wallet.address,
        relationship=copy_relationship,
        trader=copy_relationship.trader,
        ai_confidence=Decimal("0.80"),
        ai_decision="COPY",
    )
    ok, reasons = RiskEngine().reject(ctx)
    assert ok is False
    assert any("max_per_trade" in r for r in reasons)


@pytest.mark.django_db
def test_copy_daily_limit_enforced(sample_event, wallet, copy_relationship, source_trade):
    yes = sample_event.outcomes.get(outcome_type=EventOutcome.OutcomeType.YES)
    ctx = RiskContext(
        event=sample_event,
        outcome=yes,
        amount=Decimal("50"),
        wallet_address=wallet.address,
        relationship=copy_relationship,
        trader=copy_relationship.trader,
        ai_confidence=Decimal("0.80"),
        ai_decision="COPY",
        daily_copy_total=Decimal("180"),
    )
    ok, reasons = RiskEngine().reject(ctx)
    assert ok is False
    assert any("daily copy limit" in r.lower() for r in reasons)


@pytest.mark.django_db
def test_ai_cannot_bypass_risk_low_confidence(sample_event, wallet, copy_relationship):
    copy_relationship.copy_mode = CopyRelationship.CopyMode.SMART
    copy_relationship.minimum_confidence = Decimal("0.55")
    copy_relationship.save()

    yes = sample_event.outcomes.get(outcome_type=EventOutcome.OutcomeType.YES)
    ctx = RiskContext(
        event=sample_event,
        outcome=yes,
        amount=Decimal("25"),
        wallet_address=wallet.address,
        relationship=copy_relationship,
        trader=copy_relationship.trader,
        ai_confidence=Decimal("0.30"),
        ai_decision="COPY",
    )
    ok, reasons = RiskEngine().reject(ctx)
    assert ok is False
    assert any("confidence" in r.lower() for r in reasons)
