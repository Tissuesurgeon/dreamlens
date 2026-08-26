from django.contrib import admin

from .models import NetworkConfig, TransactionRecord


@admin.register(NetworkConfig)
class NetworkConfigAdmin(admin.ModelAdmin):
    list_display = ("name", "chain_id", "is_active")
    list_filter = ("is_active",)


@admin.register(TransactionRecord)
class TransactionRecordAdmin(admin.ModelAdmin):
    list_display = ("tx_hash", "user", "status", "created_at", "confirmed_at")
    list_filter = ("status",)
    search_fields = ("tx_hash", "user__username")
    readonly_fields = ("created_at",)
