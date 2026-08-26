from django.contrib import admin

from .models import Position


@admin.register(Position)
class PositionAdmin(admin.ModelAdmin):
    list_display = (
        "user",
        "event",
        "outcome",
        "amount",
        "status",
        "pnl",
        "opened_at",
    )
    list_filter = ("status",)
    search_fields = ("user__username", "event__title")
    readonly_fields = ("opened_at",)
