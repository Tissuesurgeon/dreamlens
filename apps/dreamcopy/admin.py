from django.contrib import admin

from .models import CopyExecution, CopyRelationship, TraderProfile, TraderTrade


@admin.register(TraderProfile)
class TraderProfileAdmin(admin.ModelAdmin):
    list_display = (
        "wallet_address",
        "display_name",
        "win_rate",
        "total_volume",
        "trader_score",
        "last_active_at",
    )
    search_fields = ("wallet_address", "display_name")
    list_filter = ("last_active_at",)


@admin.register(TraderTrade)
class TraderTradeAdmin(admin.ModelAdmin):
    list_display = (
        "trader",
        "event",
        "amount",
        "entry_price",
        "result",
        "opened_at",
    )
    list_filter = ("result",)
    search_fields = ("trader__wallet_address", "external_trade_id", "transaction_hash")


@admin.register(CopyRelationship)
class CopyRelationshipAdmin(admin.ModelAdmin):
    list_display = ("user", "trader", "copy_mode", "status", "auto_execute", "updated_at")
    list_filter = ("status", "copy_mode", "auto_execute")
    search_fields = ("user__username", "trader__wallet_address")
    readonly_fields = ("created_at", "updated_at")


@admin.register(CopyExecution)
class CopyExecutionAdmin(admin.ModelAdmin):
    list_display = (
        "relationship",
        "source_trade",
        "status",
        "ai_decision",
        "ai_confidence",
        "created_at",
    )
    list_filter = ("status",)
    readonly_fields = ("created_at",)
