from __future__ import annotations

from decimal import Decimal

from django.conf import settings
from django.db import models


class TraderProfile(models.Model):
    """On-chain trader profile derived from indexed fills."""

    wallet_address = models.CharField(max_length=42, unique=True)
    display_name = models.CharField(max_length=128, blank=True)
    avatar = models.URLField(blank=True)
    total_trades = models.PositiveIntegerField(default=0)
    completed_trades = models.PositiveIntegerField(default=0)
    winning_trades = models.PositiveIntegerField(default=0)
    losing_trades = models.PositiveIntegerField(default=0)
    win_rate = models.DecimalField(
        max_digits=6,
        decimal_places=4,
        default=Decimal("0"),
    )
    total_volume = models.DecimalField(
        max_digits=24,
        decimal_places=8,
        default=Decimal("0"),
    )
    realized_pnl = models.DecimalField(
        max_digits=24,
        decimal_places=8,
        default=Decimal("0"),
    )
    roi = models.DecimalField(
        max_digits=10,
        decimal_places=4,
        default=Decimal("0"),
    )
    last_active_at = models.DateTimeField(null=True, blank=True)
    metadata_json = models.JSONField(default=dict, blank=True)
    trader_score = models.DecimalField(
        max_digits=8,
        decimal_places=4,
        default=Decimal("0"),
    )

    class Meta:
        verbose_name = "trader profile"
        verbose_name_plural = "trader profiles"
        indexes = [
            models.Index(fields=["wallet_address"]),
            models.Index(fields=["trader_score"]),
        ]

    def __str__(self) -> str:
        return self.display_name or self.wallet_address


class TraderTrade(models.Model):
    """Historical trade from an indexed on-chain trader."""

    trader = models.ForeignKey(
        TraderProfile,
        on_delete=models.CASCADE,
        related_name="trades",
    )
    event = models.ForeignKey(
        "events.EventContract",
        on_delete=models.CASCADE,
        related_name="trader_trades",
    )
    outcome = models.ForeignKey(
        "events.EventOutcome",
        on_delete=models.CASCADE,
        related_name="trader_trades",
    )
    entry_price = models.DecimalField(max_digits=18, decimal_places=8)
    amount = models.DecimalField(max_digits=24, decimal_places=8)
    opened_at = models.DateTimeField()
    closed_at = models.DateTimeField(null=True, blank=True)
    result = models.CharField(max_length=32, blank=True)
    pnl = models.DecimalField(max_digits=24, decimal_places=8, null=True, blank=True)
    transaction_hash = models.CharField(max_length=66, blank=True)
    external_trade_id = models.CharField(max_length=128, blank=True)

    class Meta:
        verbose_name = "trader trade"
        verbose_name_plural = "trader trades"
        indexes = [
            models.Index(fields=["trader", "opened_at"]),
            models.Index(fields=["external_trade_id"]),
            models.Index(fields=["transaction_hash"]),
        ]

    def __str__(self) -> str:
        return f"{self.trader} — {self.amount} @ {self.entry_price}"


class CopyRelationship(models.Model):
    """User subscription to copy a trader's activity."""

    class Status(models.TextChoices):
        ACTIVE = "ACTIVE", "Active"
        PAUSED = "PAUSED", "Paused"
        STOPPED = "STOPPED", "Stopped"

    class CopyMode(models.TextChoices):
        BLIND = "BLIND", "Blind"
        SMART = "SMART", "Smart"
        CONSENSUS = "CONSENSUS", "Consensus"

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="copy_relationships",
    )
    trader = models.ForeignKey(
        TraderProfile,
        on_delete=models.CASCADE,
        related_name="copy_relationships",
    )
    status = models.CharField(
        max_length=16,
        choices=Status.choices,
        default=Status.ACTIVE,
    )
    copy_mode = models.CharField(
        max_length=16,
        choices=CopyMode.choices,
        default=CopyMode.SMART,
    )
    max_per_trade = models.DecimalField(
        max_digits=24,
        decimal_places=8,
        null=True,
        blank=True,
    )
    max_daily = models.DecimalField(
        max_digits=24,
        decimal_places=8,
        null=True,
        blank=True,
    )
    minimum_confidence = models.DecimalField(
        max_digits=6,
        decimal_places=4,
        null=True,
        blank=True,
    )
    min_copy_score = models.PositiveIntegerField(default=70)
    min_win_rate = models.DecimalField(
        max_digits=6,
        decimal_places=4,
        default=Decimal("0.65"),
    )
    min_completed_events = models.PositiveIntegerField(default=30)
    min_liquidity = models.DecimalField(
        max_digits=24,
        decimal_places=8,
        default=Decimal("1000"),
    )
    min_consensus = models.DecimalField(
        max_digits=6,
        decimal_places=4,
        default=Decimal("0.60"),
    )
    consider_json = models.JSONField(
        default=dict,
        blank=True,
        help_text="Flags: trader_confidence, historical_performance, liquidity, "
        "market_movement, consensus, copy_every",
    )
    allowed_assets_json = models.JSONField(default=list, blank=True)
    auto_execute = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "copy relationship"
        verbose_name_plural = "copy relationships"
        indexes = [
            models.Index(fields=["user", "status"]),
            models.Index(fields=["status"]),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["user", "trader"],
                name="unique_user_trader_copy",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.user} copies {self.trader} ({self.copy_mode})"


class CopyExecution(models.Model):
    """Record of a copy attempt for a source trader trade."""

    class Status(models.TextChoices):
        PENDING = "PENDING", "Pending"
        APPROVED = "APPROVED", "Approved"
        EXECUTED = "EXECUTED", "Executed"
        SKIPPED = "SKIPPED", "Skipped"
        FAILED = "FAILED", "Failed"

    relationship = models.ForeignKey(
        CopyRelationship,
        on_delete=models.CASCADE,
        related_name="executions",
    )
    source_trade = models.ForeignKey(
        TraderTrade,
        on_delete=models.CASCADE,
        related_name="copy_executions",
    )
    copied_trade = models.ForeignKey(
        "trading.Trade",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="copy_executions",
    )
    ai_decision = models.CharField(max_length=64, blank=True)
    ai_confidence = models.DecimalField(
        max_digits=6,
        decimal_places=4,
        null=True,
        blank=True,
    )
    copy_score = models.PositiveIntegerField(null=True, blank=True)
    score_json = models.JSONField(default=dict, blank=True)
    why_json = models.JSONField(default=list, blank=True)
    risks_json = models.JSONField(default=list, blank=True)
    amount = models.DecimalField(
        max_digits=24,
        decimal_places=8,
        null=True,
        blank=True,
        help_text="Notional size that would be / was copied.",
    )
    status = models.CharField(
        max_length=16,
        choices=Status.choices,
        default=Status.PENDING,
    )
    reason = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "copy execution"
        verbose_name_plural = "copy executions"
        indexes = [
            models.Index(fields=["status"]),
            models.Index(fields=["relationship", "created_at"]),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["relationship", "source_trade"],
                name="unique_copy_execution_per_source_trade",
            ),
        ]

    def __str__(self) -> str:
        return f"Copy {self.source_trade_id} → {self.status}"
