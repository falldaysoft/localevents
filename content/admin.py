"""Thin admin registrations.

The real editing surface is `/moderate/pages/` and `/moderate/media/`, which a
moderator can reach without a staff account. These exist so a superuser
debugging a site can see what is there — and, for images, so the blob columns
are visibly *not* editable here either.
"""

from django.contrib import admin

from .models import Image, Page


@admin.register(Page)
class PageAdmin(admin.ModelAdmin):
    list_display = ("title", "slug", "is_published", "show_in_footer", "updated_at")
    list_filter = ("is_published", "show_in_footer")
    search_fields = ("title", "slug", "body")
    readonly_fields = ("body_html", "created_at", "updated_at")


@admin.register(Image)
class ImageAdmin(admin.ModelAdmin):
    list_display = ("title", "width", "height", "byte_size", "uploaded_at")
    search_fields = ("title", "alt_text", "original_filename")
    readonly_fields = (
        "width",
        "height",
        "byte_size",
        "checksum",
        "content_type",
        "original_filename",
        "uploaded_at",
    )
    exclude = ("data", "thumbnail")

    def get_queryset(self, request):
        # Never pull blobs to render a changelist.
        return super().get_queryset(request).light()
