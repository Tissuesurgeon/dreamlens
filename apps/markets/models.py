from __future__ import annotations

from decimal import Decimal

from django.db import models


class MarketActivity(models.Model):
    """Rolling market activity metrics; price history lives in EventSnapshot."""

    event = models.OneToOneField(
        "events.EventContract",
        on_delete=models.CASCADE,
        related_name="market_activity",
    )
    volume = models.DecimalField(max_digits=24, decimal_places=8, default=Decimal("0"))
    liquidity = models.DecimalField(max_digits=24, decimal_places=8, default=Decimal("0"))
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "market activity"
        verbose_name_plural = "market activities"

    def __str__(self) -> str:
        return f"Activity for {self.event.title}"
