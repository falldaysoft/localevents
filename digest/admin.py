from django.contrib import admin

from .models import Subscriber


@admin.register(Subscriber)
class SubscriberAdmin(admin.ModelAdmin):
    """Read-mostly. Adding and removing people belongs to the moderator
    screen (localevents.14.4), which records who did it and why."""

    list_display = ("email", "status", "source", "user", "created_at", "last_sent_at")
    list_filter = ("source",)
    search_fields = ("email",)
    readonly_fields = (
        "token", "fingerprint", "created_at", "confirmation_sent_at",
        "confirmed_at", "unsubscribed_at", "last_sent_at",
    )
    raw_id_fields = ("user",)
