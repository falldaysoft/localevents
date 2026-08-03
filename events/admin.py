from django.contrib import admin
from django.utils.html import format_html

from .models import (
    Category,
    Event,
    GeocodeThrottle,
    Interest,
    Occurrence,
    Organizer,
    Venue,
)


class OccurrenceInline(admin.TabularInline):
    model = Occurrence
    extra = 1
    fields = ("start", "end", "is_cancelled", "note")
    ordering = ("start",)


@admin.register(Venue)
class VenueAdmin(admin.ModelAdmin):
    list_display = ("name", "city", "geocode_status", "in_region")
    list_filter = ("geocode_status", "city")
    search_fields = ("name", "address", "city", "postal_code")
    prepopulated_fields = {"slug": ("name",)}
    readonly_fields = ("geocode_attempted_at", "geocode_error", "created_at")

    @admin.display(description="In region", boolean=True)
    def in_region(self, obj):
        return obj.is_in_region

    actions = ["mark_for_regeocoding"]

    @admin.action(description="Queue selected venues for geocoding again")
    def mark_for_regeocoding(self, request, queryset):
        updated = queryset.update(
            geocode_status=Venue.GeocodeStatus.PENDING, geocode_error=""
        )
        self.message_user(request, f"{updated} venue(s) queued.")


@admin.register(Organizer)
class OrganizerAdmin(admin.ModelAdmin):
    list_display = ("name", "is_commercial", "series_usage", "contact_email")
    list_filter = ("is_commercial",)
    search_fields = ("name", "contact_email")
    prepopulated_fields = {"slug": ("name",)}

    @admin.display(description="Active series")
    def series_usage(self, obj):
        return f"{obj.active_series_count()} / {obj.max_active_series}"


@admin.register(Category)
class CategoryAdmin(admin.ModelAdmin):
    list_display = ("name", "emoji", "sort_order", "is_active", "event_count")
    list_editable = ("sort_order", "is_active")
    prepopulated_fields = {"slug": ("name",)}

    @admin.display(description="Events")
    def event_count(self, obj):
        return obj.events.count()


@admin.register(Event)
class EventAdmin(admin.ModelAdmin):
    list_display = (
        "title",
        "status",
        "prominence",
        "listing_type",
        "venue",
        "next_date",
        "interest_count",
    )
    list_filter = (
        "status",
        "prominence",
        "listing_type",
        "source",
        "is_free",
        "is_commercial",
        "categories",
    )
    search_fields = ("title", "summary", "description", "venue__name", "organizer__name")
    prepopulated_fields = {"slug": ("title",)}
    filter_horizontal = ("categories",)
    inlines = [OccurrenceInline]
    readonly_fields = ("interest_count", "renewal_token", "created_at", "published_at")
    autocomplete_fields = ("venue", "organizer")

    fieldsets = (
        (None, {"fields": ("title", "slug", "summary", "description")}),
        ("Where", {"fields": ("venue",)}),
        ("Who", {"fields": ("organizer", "submitted_by", "is_commercial")}),
        ("Links", {"fields": ("source_url", "ticket_url", "image_url")}),
        ("Cost", {"fields": ("is_free", "price_min", "price_max", "price_note")}),
        (
            "Audience",
            {"fields": ("categories", "age_min", "age_max", "is_family_friendly",
                        "accessibility_notes")},
        ),
        (
            "Placement",
            {
                "fields": ("listing_type", "prominence", "status", "source"),
                "description": (
                    "Listing type controls how the event is collapsed in a feed. "
                    "Prominence controls where it appears — a weekly market and a "
                    "weekly class are both series, but they do not belong in the "
                    "same place."
                ),
            },
        ),
        (
            "Series lifecycle",
            {
                "fields": ("series_ends_on", "renewal_token", "last_renewal_email_at"),
                "classes": ("collapse",),
            },
        ),
        (
            "Bookkeeping",
            {"fields": ("interest_count", "created_at", "published_at"),
             "classes": ("collapse",)},
        ),
    )

    @admin.display(description="Next date")
    def next_date(self, obj):
        occurrence = obj.next_occurrence()
        if occurrence is None:
            return format_html('<span style="color:#999">—</span>')
        return occurrence.start.strftime("%Y-%m-%d %H:%M")

    actions = ["publish", "feature", "send_to_background"]

    @admin.action(description="Publish selected events")
    def publish(self, request, queryset):
        updated = 0
        for event in queryset:
            event.status = Event.Status.PUBLISHED
            event.save()
            updated += 1
        self.message_user(request, f"{updated} event(s) published.")

    @admin.action(description="Feature selected events")
    def feature(self, request, queryset):
        updated = queryset.update(prominence=Event.Prominence.FEATURED)
        self.message_user(request, f"{updated} event(s) featured.")

    @admin.action(description="Move selected events to Background (programs)")
    def send_to_background(self, request, queryset):
        updated = queryset.update(prominence=Event.Prominence.BACKGROUND)
        self.message_user(request, f"{updated} event(s) moved to Background.")


@admin.register(Occurrence)
class OccurrenceAdmin(admin.ModelAdmin):
    list_display = ("event", "start", "end", "is_cancelled")
    list_filter = ("is_cancelled",)
    search_fields = ("event__title",)
    date_hierarchy = "start"


@admin.register(Interest)
class InterestAdmin(admin.ModelAdmin):
    list_display = ("event", "user", "created_at")
    search_fields = ("event__title", "user__username")
    readonly_fields = ("created_at",)


@admin.register(GeocodeThrottle)
class GeocodeThrottleAdmin(admin.ModelAdmin):
    """Visible for diagnostics — if geocoding stalls, this row explains why."""

    list_display = ("__str__", "last_called_at")

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
