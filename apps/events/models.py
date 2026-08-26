from __future__ import annotations

from decimal import Decimal

from django.db import models
from django.utils import timezone


class EventContract(models.Model):
    """Indexed DreamDEX Event Contract (binary market)."""

    class EventType(models.TextChoices):
        BINARY = "BINARY", "Binary"
        OTHER = "OTHER", "Other"

    class Status(models.TextChoices):
        LISTED = "LISTED", "Listed"
        TRADING = "TRADING", "Trading"
        LOCKED = "LOCKED", "Locked"
        SETTLING = "SETTLING", "Settling"
        RESOLVED = "RESOLVED", "Resolved"
        VOIDED = "VOIDED", "Voided"
        LIVE = "live", "Live"
        FINALIZED = "finalized", "Finalized"

    external_id = models.CharField(max_length=66, unique=True)
    title = models.CharField(max_length=512)
    description = models.TextField(blank=True)
    underlying_asset = models.CharField(max_length=32)
    event_type = models.CharField(
        max_length=16,
        choices=EventType.choices,
        default=EventType.BINARY,
    )
    status = models.CharField(
        max_length=16,
        choices=Status.choices,
        default=Status.LISTED,
    )
    expiry_time = models.DateTimeField()
    trading_start = models.DateTimeField(null=True, blank=True)
    yes_identifier = models.CharField(max_length=128)
    no_identifier = models.CharField(max_length=128)
    yes_symbol = models.CharField(max_length=128, blank=True)
    no_symbol = models.CharField(max_length=128, blank=True)
    pool_address = models.CharField(max_length=66, blank=True)
    market_address = models.CharField(max_length=66, blank=True)
    venue_id = models.CharField(max_length=66, blank=True)
    strike = models.BigIntegerField(default=0)
    interval_sec = models.PositiveIntegerField(default=900)
    cumulative_quote_volume = models.DecimalField(
        max_digits=24,
        decimal_places=8,
        default=Decimal("0"),
    )
    last_price = models.DecimalField(
        max_digits=18,
        decimal_places=8,
        null=True,
        blank=True,
    )
    trade_count = models.PositiveIntegerField(default=0)
    collateral = models.CharField(max_length=66, blank=True)
    oracle_question_id = models.CharField(max_length=128, blank=True)
    winning_outcome = models.CharField(max_length=8, blank=True)
    source = models.CharField(max_length=64, default="dreamdex")
    metadata_json = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "event contract"
        verbose_name_plural = "event contracts"
        indexes = [
            models.Index(fields=["external_id"]),
            models.Index(fields=["status"]),
            models.Index(fields=["underlying_asset", "status"]),
            models.Index(fields=["expiry_time"]),
        ]

    def __str__(self) -> str:
        return self.title

    @property
    def minutes_to_expiry(self) -> float:
        delta = self.expiry_time - timezone.now()
        return delta.total_seconds() / 60

    @property
    def opening_price(self):
        """Underlying opening/reference USD price from DreamDEX oracle, if known."""
        raw = (self.metadata_json or {}).get("opening_price")
        if raw is None or raw == "":
            return None
        try:
            return Decimal(str(raw))
        except Exception:
            return None


class EventOutcome(models.Model):
    """YES/NO outcome side for an event contract."""

    class OutcomeType(models.TextChoices):
        YES = "YES", "Yes"
        NO = "NO", "No"

    event = models.ForeignKey(
        EventContract,
        on_delete=models.CASCADE,
        related_name="outcomes",
    )
    outcome_type = models.CharField(max_length=8, choices=OutcomeType.choices)
    external_identifier = models.CharField(max_length=128)
    symbol = models.CharField(max_length=128, blank=True)
    current_price = models.DecimalField(
        max_digits=18,
        decimal_places=8,
        default=Decimal("0"),
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "event outcome"
        verbose_name_plural = "event outcomes"
        constraints = [
            models.UniqueConstraint(
                fields=["event", "outcome_type"],
                name="unique_event_outcome_type",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.event.title} — {self.outcome_type}"


class EventSnapshot(models.Model):
    """Point-in-time price and liquidity snapshot for an event."""

    event = models.ForeignKey(
        EventContract,
        on_delete=models.CASCADE,
        related_name="snapshots",
    )
    yes_price = models.DecimalField(max_digits=18, decimal_places=8)
    no_price = models.DecimalField(max_digits=18, decimal_places=8)
    volume = models.DecimalField(max_digits=24, decimal_places=8, default=Decimal("0"))
    liquidity = models.DecimalField(max_digits=24, decimal_places=8, default=Decimal("0"))
    timestamp = models.DateTimeField()

    class Meta:
        verbose_name = "event snapshot"
        verbose_name_plural = "event snapshots"
        indexes = [
            models.Index(fields=["event", "timestamp"]),
        ]
        ordering = ["-timestamp"]

    def __str__(self) -> str:
        return f"{self.event.title} @ {self.timestamp}"


class EventRadarSignal(models.Model):
    """Radar signal highlighting notable event activity or opportunity."""

    class SignalType(models.TextChoices):
        STRONG_CONSENSUS = "STRONG_CONSENSUS", "Strong consensus"
        MOVING_FAST = "MOVING_FAST", "Moving fast"
        UNUSUAL_VOLUME = "UNUSUAL_VOLUME", "Unusual volume"
        EXPIRING_SOON = "EXPIRING_SOON", "Expiring soon"
        TRADER_DIVERGENCE = "TRADER_DIVERGENCE", "Trader divergence"
        HIGH_LIQUIDITY = "HIGH_LIQUIDITY", "High liquidity"
        PRICE_IMBALANCE = "PRICE_IMBALANCE", "Price imbalance"

    event = models.ForeignKey(
        EventContract,
        on_delete=models.CASCADE,
        related_name="radar_signals",
    )
    signal_type = models.CharField(max_length=64, choices=SignalType.choices)
    score = models.DecimalField(max_digits=8, decimal_places=4)
    explanation = models.TextField(blank=True)
    details = models.JSONField(default=dict, blank=True)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "event radar signal"
        verbose_name_plural = "event radar signals"
        indexes = [
            models.Index(fields=["event", "created_at"]),
            models.Index(fields=["signal_type"]),
            models.Index(fields=["signal_type", "is_active"]),
        ]
        ordering = ["-score", "-created_at"]

    def __str__(self) -> str:
        return f"{self.signal_type} ({self.score}) — {self.event.title}"
