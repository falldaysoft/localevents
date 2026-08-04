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
    # Everything else on this model is prose. These two put arbitrary script on
    # every page and decide which origins the CSP trusts, which is a different
    # kind of decision — so they are visible to any admin and editable only by
    # someone who could already deploy the same code.
    SUPERUSER_ONLY = ("head_html", "script_hosts")

    def get_readonly_fields(self, request, obj=None):
        readonly = tuple(super().get_readonly_fields(request, obj))
        if request.user.is_superuser:
            return readonly
        return readonly + self.SUPERUSER_ONLY


@admin.register(AIConfig)
class AIConfigAdmin(SingletonAdmin):
    list_display = ("model", "enabled", "daily_spend_cap_usd")
