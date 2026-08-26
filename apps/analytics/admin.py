from django.contrib import admin

from .models import ConsensusSnapshot


@admin.register(ConsensusSnapshot)
class ConsensusSnapshotAdmin(admin.ModelAdmin):
    list_display = (
        "event",
        "yes_consensus",
        "no_consensus",
        "trader_count",
        "agreement_level",
        "created_at",
    )
    list_filter = ("created_at",)
    search_fields = ("event__title",)
    readonly_fields = ("created_at",)
