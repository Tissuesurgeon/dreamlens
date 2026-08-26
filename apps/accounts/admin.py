from django.contrib import admin
from django.contrib.auth.admin import UserAdmin as BaseUserAdmin

from .models import TelegramLink, User, Wallet


class WalletInline(admin.TabularInline):
    model = Wallet
    extra = 0
    fields = ("address", "chain_id", "is_primary", "created_at")
    readonly_fields = ("created_at",)


@admin.register(User)
class UserAdmin(BaseUserAdmin):
    list_display = ("username", "email", "is_staff", "created_at", "updated_at")
    list_filter = ("is_staff", "is_superuser", "is_active")
    search_fields = ("username", "email")
    readonly_fields = ("created_at", "updated_at", "date_joined", "last_login")
    inlines = [WalletInline]
    fieldsets = BaseUserAdmin.fieldsets + (
        ("Timestamps", {"fields": ("created_at", "updated_at")}),
    )


@admin.register(Wallet)
class WalletAdmin(admin.ModelAdmin):
    list_display = ("address", "user", "chain_id", "is_primary", "created_at")
    list_filter = ("chain_id", "is_primary")
    search_fields = ("address", "user__username")
    readonly_fields = ("created_at",)


@admin.register(TelegramLink)
class TelegramLinkAdmin(admin.ModelAdmin):
    list_display = ("chat_id", "user", "status", "linked_at", "updated_at")
    list_filter = ("status",)
    search_fields = ("chat_id", "user__username")
    readonly_fields = ("created_at", "updated_at", "linked_at")
