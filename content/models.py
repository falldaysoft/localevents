"""Pages and images, both stored entirely in the database.

The images are the unusual part, so it is worth writing down why. This site
runs as a single pod on Kubernetes with no volume mounted, which means the
container filesystem is scratch space that a deploy discards. An uploaded file
written to `MEDIA_ROOT` survives until the next release and then becomes a
broken image on a published page — the worst failure mode available, because it
looks fine for a week.

The two ways out are object storage and the database. Object storage is the
right answer at volume. This is not volume: a town's listing site accumulates a
few dozen images, normalised to at most a couple of hundred kilobytes each by
`content.images`, which is small enough that the whole media library is a
rounding error next to the events table. Putting it in Postgres means the
nightly database backup already covers it, there is no second set of
credentials, no bucket lifecycle, and no way for the two stores to disagree
about what exists.

The cost is that every byte lives in a column, so **nothing may load an image
row casually.** `Image.objects.light()` exists for that and every listing uses
it; see the note on the manager.

If this site ever grows a photo gallery, this decision should be revisited
rather than scaled — the escape route is a storage backend and a data
migration, and it is much easier while the library is still small.
"""

from django.conf import settings
from django.db import models
from django.urls import reverse
from django.utils.text import slugify

from . import render


class Page(models.Model):
    """A piece of standing prose: about, contact, house rules, a venue guide.

    Not an `Event` and deliberately unrelated to one. Everything in `events` is
    a question about dates; this is the small amount of a site that simply
    holds still.
    """

    title = models.CharField(max_length=200)
    slug = models.SlugField(
        max_length=200,
        unique=True,
        help_text="The page's web address. Changing it breaks existing links.",
    )
    body = models.TextField(
        blank=True,
        help_text="Markdown. Use the media library to add images.",
    )
    # Rendered and sanitised by `content.render` on save. Not editable: the
    # only supported way to change it is to change `body`, because that is the
    # only path that runs the sanitiser.
    body_html = models.TextField(blank=True, editable=False)

    is_published = models.BooleanField(
        default=False,
        verbose_name="published",
        help_text="Unpublished pages are visible to moderators at their address, nobody else.",
    )
    show_in_footer = models.BooleanField(
        default=True,
        verbose_name="show in the footer",
        help_text="List this page in the site footer.",
    )
    footer_order = models.SmallIntegerField(
        default=0,
        verbose_name="footer position",
        help_text="Lower numbers sort first.",
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="+",
    )

    class Meta:
        ordering = ["footer_order", "title"]

    def __str__(self):
        return self.title

    def save(self, *args, **kwargs):
        if not self.slug:
            self.slug = _slug_from(self.title, Page, self.pk)
        self.body_html = render.render(self.body)
        super().save(*args, **kwargs)

    def rerender(self):
        """Re-run the current renderer over stored source.

        `body_html` is generated at save, so a change to the allowlist in
        `content.render` does not reach pages that already exist. Anything that
        tightens those rules should sweep with this.
        """
        self.body_html = render.render(self.body)
        self.save(update_fields=["body_html", "updated_at"])

    def get_absolute_url(self):
        return reverse("page_detail", kwargs={"slug": self.slug})

    @property
    def description(self):
        return render.summarise(self.body)


class ImageQuerySet(models.QuerySet):
    def light(self):
        """Every column except the two that hold megabytes.

        The media library grid shows forty images at once. Without this it
        would pull forty full-size blobs *and* forty thumbnails out of the
        database to render forty `<img>` tags that then fetch the bytes over
        HTTP anyway — tens of megabytes moved to display none of it.

        `defer` rather than `only` so that adding a column later does not
        silently drop it from every listing.
        """
        return self.defer("data", "thumbnail")


class Image(models.Model):
    """One uploaded image, normalised — see `content.images`.

    What is stored is what is served: the original is discarded on upload, and
    `data` is already resized, re-encoded as WebP and stripped of metadata.
    `thumbnail` is a second, much smaller encoding of the same picture, which
    exists so the media library grid does not download the full library to show
    a contact sheet of it.
    """

    title = models.CharField(
        max_length=200,
        help_text="What this is, for finding it again later.",
    )
    alt_text = models.CharField(
        max_length=300,
        blank=True,
        help_text=(
            "What the image shows, for readers using a screen reader. "
            "Leave blank only if it is purely decorative."
        ),
    )

    data = models.BinaryField(editable=False)
    thumbnail = models.BinaryField(editable=False)
    content_type = models.CharField(max_length=40, editable=False)
    width = models.PositiveIntegerField(editable=False)
    height = models.PositiveIntegerField(editable=False)
    byte_size = models.PositiveIntegerField(editable=False)

    # sha256 of `data`. Serves as the ETag, and makes it cheap to notice the
    # same picture being uploaded twice.
    checksum = models.CharField(max_length=64, editable=False, db_index=True)
    original_filename = models.CharField(max_length=255, blank=True, editable=False)

    uploaded_at = models.DateTimeField(auto_now_add=True)
    uploaded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="+",
    )

    objects = ImageQuerySet.as_manager()

    class Meta:
        ordering = ["-uploaded_at"]

    def __str__(self):
        return self.title

    @property
    def filename(self):
        """A readable last path segment. Cosmetic — the pk does the routing."""
        return f"{slugify(self.title)[:60] or 'image'}.webp"

    def get_absolute_url(self):
        return reverse("image_file", kwargs={"pk": self.pk, "filename": self.filename})

    @property
    def thumbnail_url(self):
        return reverse("image_thumbnail", kwargs={"pk": self.pk})

    @property
    def markdown(self):
        """The snippet an author pastes into a page.

        Offered as a copyable string rather than an insert-at-cursor button
        because a plain textarea has no cursor to insert at, and this works
        the same whether the author is writing the page here or drafting it
        somewhere else.
        """
        return f"![{self.alt_text or self.title}]({self.get_absolute_url()})"

    def apply(self, normalised, filename=""):
        """Copy a `content.images.NormalisedImage` onto this row."""
        self.data = normalised.data
        self.thumbnail = normalised.thumbnail
        self.content_type = "image/webp"
        self.width = normalised.width
        self.height = normalised.height
        self.byte_size = normalised.byte_size
        self.checksum = normalised.checksum
        if filename:
            self.original_filename = filename[:255]


def _slug_from(title, model, pk=None):
    """Slugify, and step aside if that address is taken.

    Pages differ from events here: a moderator picks a page's address and can
    see it in the form, so a silent random suffix would be a surprise. This
    only runs when the field was left blank, and the form rejects a collision
    the author typed themselves.
    """
    base = slugify(title)[:190] or "page"
    slug = base
    suffix = 2
    while model.objects.filter(slug=slug).exclude(pk=pk).exists():
        slug = f"{base}-{suffix}"
        suffix += 1
    return slug
