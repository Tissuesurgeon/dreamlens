from __future__ import annotations

from django.conf import settings
from django.db import models


class NetworkConfig(models.Model):
    """Supported blockchain network configuration."""

    chain_id = models.PositiveIntegerField(unique=True)
    name = models.CharField(max_length=64)
    rpc_url = models.URLField()
    is_active = models.BooleanField(default=True)

    class Meta:
        verbose_name = "network config"
        verbose_name_plural = "network configs"

    def __str__(self) -> str:
        return f"{self.name} ({self.chain_id})"


class TransactionRecord(models.Model):
    """On-chain transaction tracking record."""

    class Status(models.TextChoices):
        PENDING = "PENDING", "Pending"
        CONFIRMED = "CONFIRMED", "Confirmed"
        FAILED = "FAILED", "Failed"

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="transaction_records",
    )
    tx_hash = models.CharField(max_length=66, unique=True)
    status = models.CharField(
        max_length=16,
        choices=Status.choices,
        default=Status.PENDING,
    )
    receipt_json = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    confirmed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        verbose_name = "transaction record"
        verbose_name_plural = "transaction records"
        indexes = [
            models.Index(fields=["status"]),
            models.Index(fields=["user", "status"]),
        ]

    def __str__(self) -> str:
        return f"{self.tx_hash} ({self.status})"
