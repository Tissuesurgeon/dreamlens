from __future__ import annotations

from django.contrib.auth.models import AbstractUser
from django.db import models


class User(AbstractUser):
    """DreamLens custom user model."""

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "user"
        verbose_name_plural = "users"

    def __str__(self) -> str:
        return self.username


class Wallet(models.Model):
    """Linked wallet address for a user. Never stores private keys."""

    user = models.ForeignKey(
        User,
        on_delete=models.CASCADE,
        related_name="wallets",
    )
    address = models.CharField(max_length=42)
    chain_id = models.PositiveIntegerField()
    is_primary = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "wallet"
        verbose_name_plural = "wallets"
        indexes = [
            models.Index(fields=["address"]),
            models.Index(fields=["user", "is_primary"]),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["user", "address", "chain_id"],
                name="unique_user_wallet_chain",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.address} ({self.chain_id})"


class TelegramLink(models.Model):
    """Binds a Telegram chat to a DreamLens wallet user after confirm-from-chat."""

    class Status(models.TextChoices):
        PENDING = "PENDING", "Pending"
        ACTIVE = "ACTIVE", "Active"

    user = models.OneToOneField(
        User,
        on_delete=models.CASCADE,
        related_name="telegram_link",
    )
    chat_id = models.BigIntegerField(unique=True)
    status = models.CharField(
        max_length=16,
        choices=Status.choices,
        default=Status.PENDING,
    )
    confirm_token = models.CharField(max_length=64, blank=True)
    confirm_expires_at = models.DateTimeField(null=True, blank=True)
    linked_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "telegram link"
        verbose_name_plural = "telegram links"
        indexes = [
            models.Index(fields=["chat_id", "status"]),
        ]

    def __str__(self) -> str:
        return f"{self.chat_id} ({self.status})"
