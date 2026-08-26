from __future__ import annotations

from decimal import Decimal

from django.conf import settings
from django.db import models


class Position(models.Model):
    """Open or settled position in an event outcome."""

    class Status(models.TextChoices):
        OPEN = "OPEN", "Open"
        CLOSED = "CLOSED", "Closed"
        SETTLED = "SETTLED", "Settled"

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="positions",
    )
    event = models.ForeignKey(
        "events.EventContract",
        on_delete=models.CASCADE,
        related_name="positions",
    )
    outcome = models.ForeignKey(
        "events.EventOutcome",
        on_delete=models.CASCADE,
        related_name="positions",
    )
    amount = models.DecimalField(max_digits=24, decimal_places=8)
    entry_price = models.DecimalField(max_digits=18, decimal_places=8)
    current_value = models.DecimalField(
        max_digits=24,
        decimal_places=8,
        default=Decimal("0"),
    )
    status = models.CharField(
        max_length=16,
        choices=Status.choices,
        default=Status.OPEN,
    )
    pnl = models.DecimalField(max_digits=24, decimal_places=8, null=True, blank=True)
    opened_at = models.DateTimeField(auto_now_add=True)
    settled_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        verbose_name = "position"
        verbose_name_plural = "positions"
        indexes = [
            models.Index(fields=["user", "status"]),
            models.Index(fields=["status"]),
        ]

    def __str__(self) -> str:
        return f"{self.user} — {self.outcome} ({self.status})"
