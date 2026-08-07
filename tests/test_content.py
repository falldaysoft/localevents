"""Pages and media.

Three things carry this file, and they are the three that would be expensive to
get wrong after launch.

First, **an upload is not trusted input that happens to be a picture** — it is
a file a stranger to the codebase chose from a phone. The normaliser is tested
for what it does to the awkward cases (sideways, enormous, carrying a home
address in its metadata, pretending to be an image) rather than for the happy
path, because the happy path was never the risk.

Second, **the sanitiser is the only thing between a moderator account and
stored XSS.** Page HTML is generated once and served forever after, so a hole
here is not a bug that shows up on the next request; it is a bug baked into a
row.

Third, **blobs must not be loaded by accident.** The whole justification for
images-in-Postgres is that the library is small and never fetched casually. A
listing view that quietly selects every byte turns a reasonable decision into
the wrong one, silently, at exactly the moment the site gets popular.
"""

import io

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from PIL import Image as PILImage

from content import images, render
from content.models import Image, Page


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def make_image(width=80, height=60, mode="RGB", image_format="JPEG", exif=None):
    """An in-memory image file, as a browser would send it."""
    image = PILImage.new(mode, (width, height), "red" if mode == "RGB" else None)
    buffer = io.BytesIO()
    if exif is not None:
        image.save(buffer, format=image_format, exif=exif)
    else:
        image.save(buffer, format=image_format)
    return buffer.getvalue()


def upload(content=None, name="photo.jpg", content_type="image/jpeg"):
    return SimpleUploadedFile(name, content if content is not None else make_image(), content_type)


def stored(**kwargs):
    """An `Image` row built through the real normaliser."""
    result = images.normalise(upload(**kwargs))
    image = Image(title="Test image", alt_text="A test image")
    image.apply(result, filename="photo.jpg")
    image.save()
    return image


@pytest.fixture
def signed_in_mod(client, moderator):
    client.force_login(moderator)
    return client


# ---------------------------------------------------------------------------
# Normalising an upload
# ---------------------------------------------------------------------------


def test_large_image_is_downscaled(settings):
    settings.CMS_IMAGE_MAX_DIMENSION = 200

    result = images.normalise(upload(make_image(1000, 500)))

    assert result.width == 200
    assert result.height == 100


def test_small_image_is_not_enlarged(settings):
    """`thumbnail` shrinks and never grows — a 40px logo stays 40px.

    Worth pinning: the obvious `ImageOps.contain` would scale it up to fill the
    box and produce a blurry image nobody asked for.
    """
    settings.CMS_IMAGE_MAX_DIMENSION = 1600

    result = images.normalise(upload(make_image(40, 30)))

    assert (result.width, result.height) == (40, 30)


def test_everything_is_stored_as_webp():
    result = images.normalise(upload(make_image(image_format="PNG"), name="a.png"))

    assert PILImage.open(io.BytesIO(result.data)).format == "WEBP"


def test_thumbnail_is_generated_and_smaller(settings):
    settings.CMS_IMAGE_MAX_DIMENSION = 1600
    settings.CMS_THUMBNAIL_MAX_DIMENSION = 50

    result = images.normalise(upload(make_image(800, 800)))

    assert max(PILImage.open(io.BytesIO(result.thumbnail)).size) == 50
    assert len(result.thumbnail) < len(result.data)


def test_exif_rotation_is_applied_to_the_pixels():
    """A portrait phone photo is stored landscape plus a rotate flag.

    Strip the metadata without acting on it first and the image is served on
    its side — which is what happens if these two steps are ever reordered.
    """
    exif = PILImage.Exif()
    exif[274] = 6  # Orientation: rotate 90°.

    result = images.normalise(upload(make_image(100, 50, exif=exif)))

    assert (result.width, result.height) == (50, 100)


def test_metadata_does_not_survive():
    """Including the GPS block, which is the one with a consequence.

    A photo taken at a volunteer's kitchen table carries their coordinates. It
    is published the moment it reaches a page, and nobody involved would guess.
    """
    exif = PILImage.Exif()
    exif[271] = "TestCameraMake"
    gps = exif.get_ifd(0x8825)
    gps[1] = "N"
    gps[2] = (51.0, 30.0, 0.0)

    source = make_image(100, 100, exif=exif)
    assert b"TestCameraMake" in source  # The fixture really does carry it.

    result = images.normalise(upload(source))

    reopened = PILImage.open(io.BytesIO(result.data))
    assert dict(reopened.getexif()) == {}
    assert b"TestCameraMake" not in result.data


def test_transparency_is_preserved():
    result = images.normalise(
        upload(make_image(mode="RGBA", image_format="PNG"), name="a.png")
    )

    assert PILImage.open(io.BytesIO(result.data)).mode == "RGBA"


def test_palette_image_is_handled():
    """Mode "P" is the case the metadata strip can crash on.

    `frombytes` needs self-describing pixel bytes, which a palette image's are
    not — so the conversion has to happen before the round-trip, not after.
    """
    result = images.normalise(upload(make_image(image_format="GIF"), name="a.gif"))

    assert result.width == 80


def test_svg_is_refused_by_name():
    svg = b'<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>'

    with pytest.raises(images.ImageRejected, match="SVG"):
        images.normalise(upload(svg, name="logo.svg", content_type="image/svg+xml"))


def test_a_file_that_is_not_an_image_is_refused():
    with pytest.raises(images.ImageRejected, match="does not look like an image"):
        images.normalise(upload(b"this is just text", name="notes.txt"))


def test_oversized_file_is_refused_before_it_is_read(settings):
    settings.CMS_MAX_UPLOAD_BYTES = 100

    with pytest.raises(images.ImageRejected, match="limit is"):
        images.normalise(upload(make_image(500, 500)))


def test_too_many_pixels_is_refused(settings):
    """The decompression-bomb guard, which only works if it runs first."""
    settings.CMS_MAX_UPLOAD_PIXELS = 1000

    with pytest.raises(images.ImageRejected, match="larger than this site"):
        images.normalise(upload(make_image(100, 100)))


def test_an_already_read_upload_still_works():
    """Something upstream may have left the pointer at the end of the file."""
    handle = upload()
    handle.read()

    assert images.normalise(handle).width == 80


# ---------------------------------------------------------------------------
# Rendering a page
# ---------------------------------------------------------------------------


def test_script_tags_do_not_survive():
    html = render.render("Hello <script>alert(1)</script> there")

    assert "<script" not in html
    assert "alert(1)" not in html


def test_javascript_urls_do_not_survive():
    html = render.render("[click me](javascript:alert(1))")

    assert "javascript:" not in html


def test_data_uris_do_not_survive():
    """A `data:` image is how an inline SVG gets past a tag allowlist."""
    html = render.render("![x](data:image/svg+xml;base64,PHN2Zz48L3N2Zz4=)")

    assert "data:" not in html


def test_event_handler_attributes_do_not_survive():
    html = render.render('<img src="/media/1/a.webp" onerror="alert(1)">')

    assert "onerror" not in html


def test_h1_is_demoted_rather_than_stripped():
    """Stripping would leave the text bare, which reads as broken to an author."""
    html = render.render("# Overview")

    assert "<h2>Overview</h2>" in html


def test_ordinary_markdown_survives():
    html = render.render("## Hours\n\n- Monday\n- Tuesday\n\n[Book](https://example.com)")

    assert "<h2>Hours</h2>" in html
    assert "<li>Monday</li>" in html
    assert 'href="https://example.com"' in html


def test_images_survive():
    html = render.render("![A hall](/media/3/hall.webp)")

    assert '<img alt="A hall" src="/media/3/hall.webp">' in html


def test_offsite_links_get_noopener():
    html = render.render("[out](https://example.com)")

    assert 'rel="noopener noreferrer"' in html


# ---------------------------------------------------------------------------
# The Page model
# ---------------------------------------------------------------------------


def test_page_renders_its_body_on_save(db):
    page = Page.objects.create(title="About", body="## Hello")

    assert page.body_html == "<h2>Hello</h2>"


def test_slug_is_derived_from_the_title(db):
    assert Page.objects.create(title="House Rules").slug == "house-rules"


def test_a_derived_slug_steps_around_a_collision(db):
    """Counted, not random.

    An event's slug gets a random suffix because nobody reads it; a page's
    address is chosen and seen by a moderator, so a predictable `-2` is the
    less surprising answer.
    """
    Page.objects.create(title="About")

    assert Page.objects.create(title="About").slug == "about-2"


def test_rerender_reapplies_the_current_rules(db):
    page = Page.objects.create(title="About", body="## Hi")
    Page.objects.filter(pk=page.pk).update(body_html="<script>stale</script>")

    page.refresh_from_db()
    page.rerender()

    assert page.body_html == "<h2>Hi</h2>"


# ---------------------------------------------------------------------------
# Reading a page
# ---------------------------------------------------------------------------


def test_a_published_page_is_public(client, db):
    Page.objects.create(title="About", slug="about", body="Hello", is_published=True)

    response = client.get(reverse("page_detail", kwargs={"slug": "about"}))

    assert response.status_code == 200
    assert "Hello" in response.content.decode()


def test_a_draft_page_is_not_public(client, db):
    Page.objects.create(title="Draft", slug="draft", is_published=False)

    response = client.get(reverse("page_detail", kwargs={"slug": "draft"}))

    assert response.status_code == 404


def test_a_moderator_previews_a_draft_at_its_real_address(signed_in_mod, db):
    Page.objects.create(title="Draft", slug="draft", body="Soon", is_published=False)

    response = signed_in_mod.get(reverse("page_detail", kwargs={"slug": "draft"}))

    assert response.status_code == 200
    assert "This page is a draft" in response.content.decode()


def test_published_footer_pages_reach_the_footer(client, db):
    Page.objects.create(title="Shown", slug="shown", is_published=True, show_in_footer=True)
    Page.objects.create(title="Hidden", slug="hidden", is_published=True, show_in_footer=False)
    Page.objects.create(title="Drafted", slug="drafted", is_published=False, show_in_footer=True)

    body = client.get(reverse("index")).content.decode()

    assert "Shown" in body
    assert "Hidden" not in body
    assert "Drafted" not in body


# ---------------------------------------------------------------------------
# Serving an image
# ---------------------------------------------------------------------------


def test_an_image_is_served_as_webp(client, db):
    image = stored()

    response = client.get(image.get_absolute_url())

    assert response.status_code == 200
    assert response["Content-Type"] == "image/webp"
    assert response["X-Content-Type-Options"] == "nosniff"
    assert response.content[:4] == b"RIFF"


def test_an_unchanged_image_is_not_sent_twice(client, db):
    image = stored()
    url = image.get_absolute_url()
    etag = client.get(url)["ETag"]

    response = client.get(url, headers={"if-none-match": etag})

    assert response.status_code == 304
    # Without this the browser has nothing to revalidate with next time and
    # re-downloads the whole file on every visit.
    assert response["ETag"] == etag


def test_full_size_and_thumbnail_have_different_etags(client, db):
    """Otherwise a cache that has one believes it has the other."""
    image = stored(content=make_image(600, 600))

    full = client.get(image.get_absolute_url())["ETag"]
    thumb = client.get(image.thumbnail_url)["ETag"]

    assert full != thumb


def test_the_thumbnail_route_serves_the_smaller_copy(client, db):
    image = stored(content=make_image(800, 800))

    full = client.get(image.get_absolute_url())
    thumb = client.get(image.thumbnail_url)

    assert len(thumb.content) < len(full.content)


def test_a_missing_image_is_a_404(client, db):
    assert client.get("/media/9999/x.webp").status_code == 404


# ---------------------------------------------------------------------------
# Not loading blobs by accident
# ---------------------------------------------------------------------------


def test_the_library_listing_defers_the_blobs(db):
    stored()

    listed = Image.objects.light().first()

    assert "data" in listed.get_deferred_fields()
    assert "thumbnail" in listed.get_deferred_fields()
    # The columns the grid actually renders must still be there, or deferring
    # would trade one query for forty.
    assert "width" not in listed.get_deferred_fields()


def test_the_media_page_costs_the_same_however_many_images_there_are(signed_in_mod):
    """The shape of the failure, not a magic number.

    A deferred blob is fetched lazily, so a template that touches one shows up
    as an extra query *per row* — which means the thing to assert is that the
    count does not grow with the library, not that it equals some constant a
    later change to the page chrome would break for no reason.
    """
    url = reverse("mod_media")

    # One request first, discarded. The very first hit of a test client also
    # creates the session row and the SiteConfig singleton, and counting those
    # against the smaller library made the *bigger* one look cheaper.
    signed_in_mod.get(url)

    for _ in range(2):
        stored()
    with CaptureQueriesContext(connection) as small:
        signed_in_mod.get(url)

    for _ in range(4):
        stored()
    with CaptureQueriesContext(connection) as large:
        signed_in_mod.get(url)

    assert len(large.captured_queries) == len(small.captured_queries)


# ---------------------------------------------------------------------------
# Who may do this
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "name", ["mod_pages", "mod_page_new", "mod_media"]
)
def test_the_editor_is_closed_to_the_logged_out(client, db, name):
    response = client.get(reverse(name))

    assert response.status_code == 302
    assert "/accounts/login/" in response["Location"]


@pytest.mark.parametrize(
    "name", ["mod_pages", "mod_page_new", "mod_media"]
)
def test_the_editor_is_closed_to_a_signed_in_stranger(client, submitter, name):
    client.force_login(submitter)

    assert client.get(reverse(name)).status_code == 403


def test_a_moderator_may_write_a_page(signed_in_mod, db):
    response = signed_in_mod.post(
        reverse("mod_page_new"),
        {
            "title": "House rules",
            "slug": "",
            "body": "## Be kind",
            "footer_order": 0,
            "show_in_footer": "on",
        },
    )

    assert response.status_code == 302
    page = Page.objects.get(slug="house-rules")
    assert page.body_html == "<h2>Be kind</h2>"
    assert page.is_published is False  # Nothing publishes itself.


def test_a_typed_duplicate_slug_is_rejected_rather_than_renamed(signed_in_mod, db):
    """The blank-slug path may invent a name; a typed one may not.

    Silently saving "about" as "about-2" would leave a moderator looking at a
    page that is not at the address they just typed.
    """
    Page.objects.create(title="About", slug="about")

    response = signed_in_mod.post(
        reverse("mod_page_new"),
        {"title": "About us", "slug": "about", "body": "", "footer_order": 0},
    )

    assert response.status_code == 200
    assert "already uses that address" in response.content.decode()
    assert Page.objects.count() == 1


def test_a_moderator_may_upload_an_image(signed_in_mod, db):
    response = signed_in_mod.post(
        reverse("mod_media"),
        {"title": "A hall", "alt_text": "The hall", "file": upload()},
    )

    assert response.status_code == 302
    image = Image.objects.get()
    assert image.width == 80
    assert image.content_type == "image/webp"
    assert image.byte_size > 0


def test_a_rejected_upload_explains_itself_on_the_form(signed_in_mod, db):
    response = signed_in_mod.post(
        reverse("mod_media"),
        {"title": "Logo", "file": upload(b"<svg></svg>", name="logo.svg")},
    )

    assert response.status_code == 200
    assert "SVG images can carry scripts" in response.content.decode()
    assert Image.objects.count() == 0


def test_an_untitled_upload_takes_its_name_from_the_file(signed_in_mod, db):
    signed_in_mod.post(
        reverse("mod_media"),
        {"title": "", "file": upload(name="village_fete.jpg")},
    )

    assert Image.objects.get().title == "village fete"


def test_deleting_an_image_a_page_uses_asks_first(signed_in_mod, db):
    image = stored()
    Page.objects.create(title="About", slug="about", body=image.markdown)

    response = signed_in_mod.post(reverse("mod_image_delete", kwargs={"pk": image.pk}))

    assert response.status_code == 302
    assert Image.objects.count() == 1

    signed_in_mod.post(
        reverse("mod_image_delete", kwargs={"pk": image.pk}), {"confirm": "1"}
    )
    assert Image.objects.count() == 0


def test_deleting_an_unused_image_needs_no_confirmation(signed_in_mod, db):
    image = stored()

    signed_in_mod.post(reverse("mod_image_delete", kwargs={"pk": image.pk}))

    assert Image.objects.count() == 0


def test_the_editor_renders_no_alpine(signed_in_mod, db):
    """Same rule as the moderation queue: Alpine loads and cannot run.

    An `x-` attribute here would look right in review and do nothing in a
    browser, which is the worst combination available.
    """
    body = signed_in_mod.get(reverse("mod_pages")).content.decode()

    assert "x-data" not in body
    assert "x-show" not in body
