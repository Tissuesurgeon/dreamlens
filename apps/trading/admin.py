from django.contrib import admin

from .models import Trade


@admin.register(Trade)
class TradeAdmin(admin.ModelAdmin):
    list_display = (
        "user",
        "event",
        "side",
        "amount",
        "entry_price",
        "status",
        "opened_at",
    )
    list_filter = ("status", "side")
    search_fields = ("user__username", "external_trade_id", "transaction_hash")
    readonly_fields = ("opened_at",)
