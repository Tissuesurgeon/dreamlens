from __future__ import annotations

from decimal import Decimal

from django.conf import settings
from django.db import models


class Trade(models.Model):
    """User trade against a DreamDEX Event Contract."""

    class Side(models.TextChoices):
        BUY = "BUY", "Buy"
        SELL = "SELL", "Sell"

    class Status(models.TextChoices):
        PREPARED = "PREPARED", "Prepared"
        AWAITING_CONFIRMATION = "AWAITING_CONFIRMATION", "Awaiting confirmation"
        SUBMITTED = "SUBMITTED", "Submitted"
        CONFIRMED = "CONFIRMED", "Confirmed"
        FAILED = "FAILED", "Failed"
        EXPIRED = "EXPIRED", "Expired"

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="trades",
    )
    event = models.ForeignKey(
        "events.EventContract",
        on_delete=models.CASCADE,
        related_name="trades",
    )
    outcome = models.ForeignKey(
        "events.EventOutcome",
        on_delete=models.CASCADE,
        related_name="trades",
    )
    side = models.CharField(max_length=8, choices=Side.choices)
    amount = models.DecimalField(max_digits=24, decimal_places=8)
    entry_price = models.DecimalField(max_digits=18, decimal_places=8)
    external_trade_id = models.CharField(max_length=128, blank=True)
    transaction_hash = models.CharField(max_length=66, blank=True)
    status = models.CharField(
        max_length=32,
        choices=Status.choices,
        default=Status.PREPARED,
    )
    opened_at = models.DateTimeField(auto_now_add=True)
    settled_at = models.DateTimeField(null=True, blank=True)
    result = models.CharField(max_length=32, blank=True)
    pnl = models.DecimalField(max_digits=24, decimal_places=8, null=True, blank=True)
    metadata_json = models.JSONField(default=dict, blank=True)

    class Meta:
        verbose_name = "trade"
        verbose_name_plural = "trades"
        indexes = [
            models.Index(fields=["status"]),
            models.Index(fields=["external_trade_id"]),
            models.Index(fields=["transaction_hash"]),
            models.Index(fields=["user", "status"]),
        ]

    def __str__(self) -> str:
        return f"{self.side} {self.amount} @ {self.entry_price} ({self.status})"
