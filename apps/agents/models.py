# DreamLens Smart Account + DreamAgent models.
# Keeps legacy chat session models; adds authority / lifecycle entities.

from __future__ import annotations

from decimal import Decimal

from django.conf import settings
from django.db import models
from django.utils import timezone


class AgentSession(models.Model):
    """AI agent conversation session."""

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="agent_sessions",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "agent session"
        verbose_name_plural = "agent sessions"
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"Session {self.pk} ({self.created_at})"


class AgentMessage(models.Model):
    """Message within an agent session."""

    class Role(models.TextChoices):
        SYSTEM = "system", "System"
        USER = "user", "User"
        ASSISTANT = "assistant", "Assistant"
        TOOL = "tool", "Tool"

    session = models.ForeignKey(
        AgentSession,
        on_delete=models.CASCADE,
        related_name="messages",
    )
    role = models.CharField(max_length=16, choices=Role.choices)
    content = models.TextField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "agent message"
        verbose_name_plural = "agent messages"
        ordering = ["created_at"]

    def __str__(self) -> str:
        return f"{self.role}: {self.content[:50]}"


class AgentDecision(models.Model):
    """Structured AI decision for trading or copy actions."""

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="agent_decisions",
    )
    event = models.ForeignKey(
        "events.EventContract",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="agent_decisions",
    )
    action = models.CharField(max_length=64)
    confidence = models.DecimalField(max_digits=6, decimal_places=4)
    reasoning = models.TextField()
    structured_output_json = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "agent decision"
        verbose_name_plural = "agent decisions"
        indexes = [
            models.Index(fields=["user", "created_at"]),
            models.Index(fields=["event", "created_at"]),
        ]
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"{self.action} ({self.confidence})"


class AgentToolCall(models.Model):
    """Tool invocation during an agent session."""

    session = models.ForeignKey(
        AgentSession,
        on_delete=models.CASCADE,
        related_name="tool_calls",
    )
    message = models.ForeignKey(
        AgentMessage,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="tool_calls",
    )
    tool_name = models.CharField(max_length=128)
    arguments_json = models.JSONField(default=dict)
    result_json = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "agent tool call"
        verbose_name_plural = "agent tool calls"
        ordering = ["created_at"]

    def __str__(self) -> str:
        return f"{self.tool_name} in session {self.session_id}"


# ---------------------------------------------------------------------------
# DreamLens Smart Account + DreamAgent (authority layer)
# ---------------------------------------------------------------------------


class SmartAccount(models.Model):
    """User-owned MetaMask Hybrid Smart Account (DreamLens Smart Account)."""

    class Status(models.TextChoices):
        PENDING = "PENDING", "Pending"
        DEPLOYED = "DEPLOYED", "Deployed"
        FUNDED = "FUNDED", "Funded"

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="smart_accounts",
    )
    owner_address = models.CharField(max_length=42)
    address = models.CharField(max_length=42)
    chain_id = models.PositiveIntegerField()
    factory_address = models.CharField(max_length=42, blank=True)
    deploy_salt = models.CharField(max_length=66, default="0x")
    implementation = models.CharField(max_length=32, default="Hybrid")
    status = models.CharField(
        max_length=16,
        choices=Status.choices,
        default=Status.PENDING,
    )
    metadata_json = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "smart account"
        verbose_name_plural = "smart accounts"
        indexes = [
            models.Index(fields=["user", "chain_id"]),
            models.Index(fields=["address"]),
            models.Index(fields=["owner_address"]),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["user", "chain_id"],
                name="unique_user_smart_account_chain",
            ),
            models.UniqueConstraint(
                fields=["address", "chain_id"],
                name="unique_smart_account_address_chain",
            ),
        ]

    def __str__(self) -> str:
        return f"SA {self.address} (owner={self.owner_address})"


class DreamAgent(models.Model):
    """Autonomous Smart Copy agent with delegated (never owner) authority."""

    class Status(models.TextChoices):
        CREATED = "CREATED", "Created"
        FUNDED = "FUNDED", "Funded"
        CONFIGURED = "CONFIGURED", "Configured"
        AUTHORIZED = "AUTHORIZED", "Authorized"
        RUNNING = "RUNNING", "Running"
        PAUSED = "PAUSED", "Paused"
        EXPIRED = "EXPIRED", "Expired"
        REVOKED = "REVOKED", "Revoked"

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="dream_agents",
    )
    smart_account = models.ForeignKey(
        SmartAccount,
        on_delete=models.CASCADE,
        related_name="agents",
    )
    name = models.CharField(max_length=128, default="DreamAgent")
    session_address = models.CharField(
        max_length=42,
        help_text="Public address of the session EOA delegate (never stores private keys here).",
    )
    status = models.CharField(
        max_length=16,
        choices=Status.choices,
        default=Status.CREATED,
    )
    initial_capital = models.DecimalField(
        max_digits=24,
        decimal_places=8,
        default=Decimal("0"),
    )
    metadata_json = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "dream agent"
        verbose_name_plural = "dream agents"
        indexes = [
            models.Index(fields=["user", "status"]),
            models.Index(fields=["session_address"]),
        ]

    def __str__(self) -> str:
        return f"{self.name} ({self.status})"

    @property
    def is_autonomous(self) -> bool:
        return self.status == self.Status.RUNNING

    def can_transition_to(self, new_status: str) -> bool:
        if new_status == self.Status.REVOKED:
            return self.status != self.Status.REVOKED
        allowed: dict[str, set[str]] = {
            self.Status.CREATED: {self.Status.FUNDED, self.Status.CONFIGURED},
            self.Status.FUNDED: {self.Status.CONFIGURED},
            self.Status.CONFIGURED: {self.Status.AUTHORIZED},
            self.Status.AUTHORIZED: {self.Status.RUNNING, self.Status.PAUSED},
            self.Status.RUNNING: {
                self.Status.PAUSED,
                self.Status.EXPIRED,
                self.Status.AUTHORIZED,
            },
            self.Status.PAUSED: {self.Status.RUNNING, self.Status.AUTHORIZED},
            self.Status.EXPIRED: {self.Status.CONFIGURED, self.Status.AUTHORIZED},
            self.Status.REVOKED: set(),
        }
        return new_status in allowed.get(self.status, set())


class DreamAgentPermission(models.Model):
    """Bounded TRADE_EVENT_CONTRACT delegation from Smart Account → DreamAgent."""

    class Status(models.TextChoices):
        PENDING = "PENDING", "Pending"
        ACTIVE = "ACTIVE", "Active"
        EXPIRED = "EXPIRED", "Expired"
        REVOKED = "REVOKED", "Revoked"

    class PermissionType(models.TextChoices):
        TRADE_EVENT_CONTRACT = "TRADE_EVENT_CONTRACT", "Trade Event Contract"

    owner_address = models.CharField(max_length=42)
    agent = models.ForeignKey(
        DreamAgent,
        on_delete=models.CASCADE,
        related_name="permissions",
    )
    smart_account = models.ForeignKey(
        SmartAccount,
        on_delete=models.CASCADE,
        related_name="permissions",
    )
    chain_id = models.PositiveIntegerField()
    permission_type = models.CharField(
        max_length=32,
        choices=PermissionType.choices,
        default=PermissionType.TRADE_EVENT_CONTRACT,
    )
    max_trade_amount = models.DecimalField(
        max_digits=24,
        decimal_places=8,
        default=Decimal("10"),
    )
    max_daily_volume = models.DecimalField(
        max_digits=24,
        decimal_places=8,
        default=Decimal("50"),
    )
    min_copy_score = models.PositiveIntegerField(default=75)
    allowed_traders_json = models.JSONField(
        default=list,
        blank=True,
        help_text="TraderProfile IDs or wallet addresses the agent may copy.",
    )
    allowed_outcomes_json = models.JSONField(default=list, blank=True)
    allowed_contracts_json = models.JSONField(
        default=list,
        blank=True,
        help_text="DreamDEX pool addresses allowed as delegation targets.",
    )
    caveats_json = models.JSONField(default=dict, blank=True)
    signed_delegation_json = models.JSONField(
        default=dict,
        blank=True,
        help_text="Signed ERC-7710 delegation blob for redeemDelegations.",
    )
    delegation_hash = models.CharField(max_length=66, blank=True)
    expires_at = models.DateTimeField()
    status = models.CharField(
        max_length=16,
        choices=Status.choices,
        default=Status.PENDING,
    )
    revoked_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "dream agent permission"
        verbose_name_plural = "dream agent permissions"
        indexes = [
            models.Index(fields=["agent", "status"]),
            models.Index(fields=["status", "expires_at"]),
        ]

    def __str__(self) -> str:
        return f"{self.permission_type} → {self.agent_id} ({self.status})"

    @property
    def is_valid(self) -> bool:
        if self.status != self.Status.ACTIVE:
            return False
        return self.expires_at > timezone.now()


class AgentEvaluation(models.Model):
    """Every watched trade the agent evaluated (copied or skipped) — trust log."""

    class Decision(models.TextChoices):
        COPY = "COPY", "Copy"
        SKIPPED = "SKIPPED", "Skipped"
        FAILED = "FAILED", "Failed"

    agent = models.ForeignKey(
        DreamAgent,
        on_delete=models.CASCADE,
        related_name="evaluations",
    )
    source_trade = models.ForeignKey(
        "dreamcopy.TraderTrade",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="agent_evaluations",
    )
    copy_execution = models.ForeignKey(
        "dreamcopy.CopyExecution",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="agent_evaluations",
    )
    decision = models.CharField(max_length=16, choices=Decision.choices)
    copy_score = models.PositiveIntegerField(null=True, blank=True)
    trader_score = models.DecimalField(
        max_digits=8,
        decimal_places=4,
        null=True,
        blank=True,
    )
    event_score = models.DecimalField(
        max_digits=8,
        decimal_places=4,
        null=True,
        blank=True,
    )
    consensus_score = models.DecimalField(
        max_digits=8,
        decimal_places=4,
        null=True,
        blank=True,
    )
    amount = models.DecimalField(
        max_digits=24,
        decimal_places=8,
        null=True,
        blank=True,
    )
    skip_reasons_json = models.JSONField(default=list, blank=True)
    policy_json = models.JSONField(default=dict, blank=True)
    risk_json = models.JSONField(default=dict, blank=True)
    tx_hash = models.CharField(max_length=66, blank=True)
    event_title = models.CharField(max_length=256, blank=True)
    outcome = models.CharField(max_length=16, blank=True)
    trader_name = models.CharField(max_length=128, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "agent evaluation"
        verbose_name_plural = "agent evaluations"
        indexes = [
            models.Index(fields=["agent", "created_at"]),
            models.Index(fields=["agent", "decision"]),
        ]
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"{self.agent_id} {self.decision} score={self.copy_score}"
