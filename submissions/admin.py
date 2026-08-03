from django.contrib import admin

from .models import (
    ModerationAction,
    Submission,
    SubmissionMessage,
    SubmissionQuota,
)


class SubmissionMessageInline(admin.TabularInline):
    model = SubmissionMessage
    extra = 0
    fields = ("author", "is_from_moderator", "body", "created_at")
    readonly_fields = ("created_at",)


@admin.register(Submission)
class SubmissionAdmin(admin.ModelAdmin):
    list_display = ("__str__", "submitted_by", "status", "created_at", "event")
    list_filter = ("status", "enrichment_failed", "created_at")
    search_fields = ("source_url", "submitted_by__username", "submitted_by__email")
    readonly_fields = ("draft", "created_at", "updated_at")
    inlines = [SubmissionMessageInline]
    autocomplete_fields = ("event",)


@admin.register(ModerationAction)
class ModerationActionAdmin(admin.ModelAdmin):
    """Append-only by design — a decision log nobody can revise is the point."""

    list_display = ("created_at", "actor", "action", "detail")
    list_filter = ("action",)
    search_fields = ("detail", "actor__username")
    date_hierarchy = "created_at"
    readonly_fields = [f.name for f in ModerationAction._meta.fields]

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(SubmissionQuota)
class SubmissionQuotaAdmin(admin.ModelAdmin):
    list_display = ("user", "submissions_per_day", "enrichments_per_day")
    search_fields = ("user__username", "user__email")
