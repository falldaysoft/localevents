from django.contrib import admin
from django.db.models import Avg, Count, Sum

from .models import EnrichmentRun


@admin.register(EnrichmentRun)
class EnrichmentRunAdmin(admin.ModelAdmin):
    """The record that answers "is the cheap model good enough".

    Read-only on purpose: this is evidence, and editing it would defeat the
    point of keeping it.
    """

    list_display = (
        "created_at", "method", "status", "model", "endpoint", "tokens", "cost", "duration_ms",
    )
    list_filter = ("method", "status", "endpoint", "model")
    search_fields = ("source_url", "error", "model")
    date_hierarchy = "created_at"
    readonly_fields = [f.name for f in EnrichmentRun._meta.fields]

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    @admin.display(description="Tokens")
    def tokens(self, obj):
        if obj.was_free:
            return "—"
        return f"{obj.input_tokens:,} in / {obj.output_tokens:,} out"

    @admin.display(description="Est. cost")
    def cost(self, obj):
        if obj.was_free:
            return "free"
        return f"${obj.estimated_cost_usd:.4f}"

    def changelist_view(self, request, extra_context=None):
        """Show the totals that actually inform a decision."""
        queryset = self.get_queryset(request)
        summary = queryset.aggregate(
            runs=Count("id"),
            spend=Sum("estimated_cost_usd"),
            mean_ms=Avg("duration_ms"),
        )
        free = queryset.filter(method=EnrichmentRun.Method.STRUCTURED).count()

        extra_context = extra_context or {}
        extra_context["summary"] = {
            **summary,
            "free_runs": free,
            "free_share": (free / summary["runs"] * 100) if summary["runs"] else 0,
        }
        return super().changelist_view(request, extra_context)
