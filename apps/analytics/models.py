from __future__ import annotations

from decimal import Decimal

from django.db import models


class ConsensusSnapshot(models.Model):
    """Trader consensus snapshot for an event at a point in time."""

    event = models.ForeignKey(
        "events.EventContract",
        on_delete=models.CASCADE,
        related_name="consensus_snapshots",
    )
    yes_consensus = models.DecimalField(max_digits=18, decimal_places=8)
    no_consensus = models.DecimalField(max_digits=18, decimal_places=8)
    trader_count = models.PositiveIntegerField(default=0)
    agreement_level = models.DecimalField(max_digits=6, decimal_places=4, default=Decimal("0"))
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "consensus snapshot"
        verbose_name_plural = "consensus snapshots"
        indexes = [
            models.Index(fields=["event", "created_at"]),
        ]
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"Consensus for {self.event.title} @ {self.created_at}"
