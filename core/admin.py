from django.contrib import admin

from .models import AIConfig, SiteConfig


class SingletonAdmin(admin.ModelAdmin):
    """Admin for a one-row model: no add, no delete, no changelist worth showing."""

    def has_add_permission(self, request):
        return not self.model.objects.exists()

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(SiteConfig)
class SiteConfigAdmin(SingletonAdmin):
    pass


@admin.register(AIConfig)
class AIConfigAdmin(SingletonAdmin):
    list_display = ("provider", "model", "enabled", "daily_spend_cap_usd")
