from .models import Page


def footer_pages(request):
    """Published pages that asked to appear in the footer.

    Wrapped in try/except for the same reason `core.context_processors.site_head`
    is: this runs while rendering error pages too, including the one shown when
    the database is unreachable, and a context processor that raises turns a
    handled 500 into an unhandled one.

    Only three columns are read, and `Page` holds no blobs, so this is a small
    query on a table with a handful of rows. It is still a query on every HTML
    response — worth knowing before adding a fourth thing to the footer.
    """
    try:
        return {
            "FOOTER_PAGES": Page.objects.filter(
                is_published=True, show_in_footer=True
            ).only("slug", "title")
        }
    except Exception:
        return {"FOOTER_PAGES": []}
