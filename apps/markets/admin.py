from django.contrib import admin

from .models import MarketActivity


@admin.register(MarketActivity)
class MarketActivityAdmin(admin.ModelAdmin):
    list_display = ("event", "volume", "liquidity", "updated_at")
    search_fields = ("event__title",)
    readonly_fields = ("updated_at",)
