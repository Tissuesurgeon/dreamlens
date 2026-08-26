from django.contrib import admin

from .models import (
    AgentDecision,
    AgentEvaluation,
    AgentMessage,
    AgentSession,
    AgentToolCall,
    DreamAgent,
    DreamAgentPermission,
    SmartAccount,
)


class AgentMessageInline(admin.TabularInline):
    model = AgentMessage
    extra = 0
    readonly_fields = ("created_at",)


@admin.register(AgentSession)
class AgentSessionAdmin(admin.ModelAdmin):
    list_display = ("id", "user", "created_at")
    list_filter = ("created_at",)
    search_fields = ("user__username",)
    readonly_fields = ("created_at",)
    inlines = [AgentMessageInline]


@admin.register(AgentMessage)
class AgentMessageAdmin(admin.ModelAdmin):
    list_display = ("session", "role", "created_at")
    list_filter = ("role",)
    search_fields = ("content",)


@admin.register(AgentDecision)
class AgentDecisionAdmin(admin.ModelAdmin):
    list_display = ("action", "user", "event", "confidence", "created_at")
    list_filter = ("action",)
    search_fields = ("reasoning", "user__username")
    readonly_fields = ("created_at",)


@admin.register(AgentToolCall)
class AgentToolCallAdmin(admin.ModelAdmin):
    list_display = ("session", "tool_name", "message", "created_at")
    search_fields = ("tool_name",)
    readonly_fields = ("created_at",)


@admin.register(SmartAccount)
class SmartAccountAdmin(admin.ModelAdmin):
    list_display = ("address", "owner_address", "user", "chain_id", "status", "updated_at")
    list_filter = ("status", "chain_id")
    search_fields = ("address", "owner_address", "user__username")
    readonly_fields = ("created_at", "updated_at")


class DreamAgentPermissionInline(admin.TabularInline):
    model = DreamAgentPermission
    extra = 0
    readonly_fields = ("created_at", "updated_at")
    fields = (
        "status",
        "max_trade_amount",
        "max_daily_volume",
        "min_copy_score",
        "expires_at",
        "delegation_hash",
    )


@admin.register(DreamAgent)
class DreamAgentAdmin(admin.ModelAdmin):
    list_display = ("name", "user", "status", "session_address", "updated_at")
    list_filter = ("status",)
    search_fields = ("name", "session_address", "user__username")
    readonly_fields = ("created_at", "updated_at")
    inlines = [DreamAgentPermissionInline]


@admin.register(DreamAgentPermission)
class DreamAgentPermissionAdmin(admin.ModelAdmin):
    list_display = (
        "agent",
        "permission_type",
        "status",
        "max_trade_amount",
        "max_daily_volume",
        "expires_at",
    )
    list_filter = ("status", "permission_type")
    search_fields = ("owner_address", "delegation_hash")
    readonly_fields = ("created_at", "updated_at", "revoked_at")


@admin.register(AgentEvaluation)
class AgentEvaluationAdmin(admin.ModelAdmin):
    list_display = (
        "agent",
        "decision",
        "copy_score",
        "amount",
        "trader_name",
        "event_title",
        "created_at",
    )
    list_filter = ("decision",)
    search_fields = ("trader_name", "event_title", "tx_hash")
    readonly_fields = ("created_at",)
