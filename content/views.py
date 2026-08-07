"""Two audiences in one file: moderators who write, and everyone who reads.

The split is by decorator, not by module, because the pairs belong together —
the view that stores an image and the view that serves it agree about content
type and caching, and separating them by a directory is how those two drift.
Everything under `/moderate/` carries `moderator_required`; the three public
views carry nothing and say so.
"""

from django.contrib import messages
from django.http import Http404, HttpResponse, HttpResponseNotModified
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from core.models import SiteConfig
from moderation.permissions import moderator_required

from .forms import ImageDetailsForm, ImageUploadForm, PageForm
from .models import Image, Page

# A week. The bytes behind a given id never change — `ImageDetailsForm` edits
# the caption, never the pixels — so this could in principle be a year and
# `immutable`. It is not, because that promise would be quietly broken the day
# someone adds a replace-image button, and a year of stale caches is not a
# mistake you can take back.
IMAGE_MAX_AGE = 60 * 60 * 24 * 7


# ---------------------------------------------------------------------------
# Public
# ---------------------------------------------------------------------------


def page_detail(request, slug):
    """A published page — or an unpublished one, to a moderator.

    The preview is the same view rather than a separate `?preview` route so
    that what an author checks before publishing is literally the page, not a
    second rendering of it that can disagree.
    """
    page = get_object_or_404(Page, slug=slug)

    if not page.is_published:
        if not (request.user.is_authenticated and request.user.is_moderator):
            raise Http404("No such page.")

    return render(
        request,
        "content/page_detail.html",
        {"page": page, "site_config": SiteConfig.load()},
    )


def image_file(request, pk, filename=None):
    """The stored image. `filename` is decoration; the id does the routing."""
    return _serve(request, pk, "data")


def image_thumbnail(request, pk):
    return _serve(request, pk, "thumbnail")


def _serve(request, pk, field):
    """Send one blob column, with a conditional-request fast path.

    Loading exactly one blob and not the other matters: `only()` here is the
    difference between sending a 12KB thumbnail and reading its 200KB sibling
    out of the database to throw away.

    The ETag is the checksum of the full-size image in both cases, which is
    fine — it identifies the upload, and both derivatives change together or
    not at all.
    """
    image = get_object_or_404(
        Image.objects.only(field, "content_type", "checksum"), pk=pk
    )

    etag = f'"{image.checksum}-{field}"'
    if request.headers.get("If-None-Match") == etag:
        # A 304 must still carry the validator, or the next request has nothing
        # to revalidate with and downloads the whole thing again.
        not_modified = HttpResponseNotModified()
        not_modified["ETag"] = etag
        not_modified["Cache-Control"] = f"public, max-age={IMAGE_MAX_AGE}"
        return not_modified

    # psycopg hands back a memoryview for a bytea column while SQLite returns
    # bytes. Both work locally and only one of them is what production does.
    payload = bytes(getattr(image, field))

    response = HttpResponse(payload, content_type=image.content_type)
    response["ETag"] = etag
    response["Cache-Control"] = f"public, max-age={IMAGE_MAX_AGE}"
    response["Content-Length"] = str(len(payload))
    # Stored images are stripped and re-encoded by `content.images`, so the
    # declared type is one this server produced rather than one an uploader
    # claimed. nosniff keeps a browser from second-guessing that.
    response["X-Content-Type-Options"] = "nosniff"
    return response


# ---------------------------------------------------------------------------
# Moderator
# ---------------------------------------------------------------------------


def _chrome(request, **extra):
    """Match the surrounding moderation screens.

    Imported rather than reimplemented so the queue badge on the tab strip is
    live on these pages too — a moderator who wanders into the page editor
    should still see that six submissions arrived.
    """
    from moderation import services

    return {
        "queue_counts": services.queue_counts(request.user),
        "site_config": SiteConfig.load(),
        **extra,
    }


@moderator_required
def pages(request):
    return render(
        request,
        "content/mod_pages.html",
        _chrome(request, section="pages", pages=Page.objects.all()),
    )


@moderator_required
def page_new(request):
    form = PageForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        page = form.save(commit=False)
        page.updated_by = request.user
        page.save()
        messages.success(request, f"Created “{page.title}”.")
        return redirect("mod_page_edit", pk=page.pk)

    return render(
        request,
        "content/mod_page_edit.html",
        _chrome(
            request,
            section="pages",
            form=form,
            page=None,
            library=Image.objects.light()[:24],
        ),
    )


@moderator_required
def page_edit(request, pk):
    page = get_object_or_404(Page, pk=pk)
    form = PageForm(request.POST or None, instance=page)

    if request.method == "POST" and form.is_valid():
        page = form.save(commit=False)
        page.updated_by = request.user
        page.save()
        messages.success(request, f"Saved “{page.title}”.")
        return redirect("mod_page_edit", pk=page.pk)

    return render(
        request,
        "content/mod_page_edit.html",
        _chrome(
            request,
            section="pages",
            form=form,
            page=page,
            library=Image.objects.light()[:24],
        ),
    )


@moderator_required
@require_POST
def page_delete(request, pk):
    page = get_object_or_404(Page, pk=pk)
    title = page.title
    page.delete()
    messages.success(request, f"Deleted “{title}”.")
    return redirect("mod_pages")


@moderator_required
def media(request):
    """The library, and the upload form that fills it."""
    form = ImageUploadForm(request.POST or None, request.FILES or None)

    if request.method == "POST" and form.is_valid():
        image = form.save(commit=False)
        image.uploaded_by = request.user
        image.save()
        messages.success(request, f"Uploaded “{image.title}”.")
        return redirect("mod_media")

    return render(
        request,
        "content/mod_media.html",
        _chrome(request, section="media", form=form, images=Image.objects.light()),
    )


@moderator_required
def image_edit(request, pk):
    image = get_object_or_404(Image.objects.light(), pk=pk)
    form = ImageDetailsForm(request.POST or None, instance=image)

    if request.method == "POST" and form.is_valid():
        form.save()
        messages.success(request, "Saved.")
        return redirect("mod_media")

    return render(
        request,
        "content/mod_image_edit.html",
        _chrome(request, section="media", form=form, image=image),
    )


@moderator_required
@require_POST
def image_delete(request, pk):
    """Delete, having said where it is still in use.

    Pages embed images by URL inside Markdown, so there is no foreign key to
    protect this and no cascade to reason about — a delete just turns every
    embed into a broken image. The check below is a text search, which is
    exactly as precise as the reference it is looking for.
    """
    image = get_object_or_404(Image.objects.light(), pk=pk)
    used_on = list(
        Page.objects.filter(body__contains=image.get_absolute_url()).values_list(
            "title", flat=True
        )[:5]
    )

    if used_on and "confirm" not in request.POST:
        messages.error(
            request,
            f"“{image.title}” is still used on {', '.join(used_on)}. "
            "Remove it from those pages first, or confirm to delete anyway.",
        )
        return redirect("mod_image_edit", pk=pk)

    title = image.title
    image.delete()
    messages.success(request, f"Deleted “{title}”.")
    return redirect("mod_media")
