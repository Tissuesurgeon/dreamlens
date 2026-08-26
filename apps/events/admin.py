from django.contrib import admin

from .models import EventContract, EventOutcome, EventRadarSignal, EventSnapshot


class EventOutcomeInline(admin.TabularInline):
    model = EventOutcome
    extra = 0


class EventSnapshotInline(admin.TabularInline):
    model = EventSnapshot
    extra = 0
    readonly_fields = ("timestamp",)
    fields = ("yes_price", "no_price", "volume", "liquidity", "timestamp")


@admin.register(EventContract)
class EventContractAdmin(admin.ModelAdmin):
    list_display = (
        "title",
        "underlying_asset",
        "status",
        "expiry_time",
        "external_id",
        "updated_at",
    )
    list_filter = ("status", "event_type", "underlying_asset", "source")
    search_fields = ("title", "external_id", "underlying_asset")
    readonly_fields = ("created_at", "updated_at")
    inlines = [EventOutcomeInline, EventSnapshotInline]


@admin.register(EventOutcome)
class EventOutcomeAdmin(admin.ModelAdmin):
    list_display = ("event", "outcome_type", "current_price", "external_identifier")
    list_filter = ("outcome_type",)
    search_fields = ("event__title", "external_identifier")


@admin.register(EventSnapshot)
class EventSnapshotAdmin(admin.ModelAdmin):
    list_display = ("event", "yes_price", "no_price", "volume", "timestamp")
    list_filter = ("timestamp",)
    search_fields = ("event__title",)


@admin.register(EventRadarSignal)
class EventRadarSignalAdmin(admin.ModelAdmin):
    list_display = ("event", "signal_type", "score", "is_active", "created_at")
    list_filter = ("signal_type", "is_active")
    search_fields = ("event__title", "explanation")
