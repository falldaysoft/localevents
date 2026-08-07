"""Turn whatever someone uploaded into one predictable stored image.

Submitters and moderators upload what their phone or camera gave them: a 4000px
HEIC, a 12MB PNG screenshot, a photo that is sideways because the orientation
is in EXIF rather than in the pixels. None of that should reach the database,
and none of it should reach a reader's browser.

So every upload is normalised on the way in and the original is *not* kept.
That is a deliberate trade. Keeping originals is the more forgiving choice —
you can re-derive any size later — but the whole point of this design is that
images live in Postgres rather than on a disk that a pod restart forgets, and
holding an 12MB original alongside its 180KB derivative to enable a resize
nobody has asked for is how that decision turns into a bad one. What is stored
is what is served.

Every step below exists because of something an upload actually does:

- **EXIF orientation is applied, then all metadata is dropped.** Applied
  because a portrait phone photo is stored as landscape-plus-a-rotate-flag, and
  anything that reads the pixels without the flag renders it on its side.
  Dropped because the same block carries GPS coordinates, and a photo taken at
  a volunteer's home would otherwise publish their address to anyone who runs
  exiftool on it. This is the one step with a privacy consequence.
- **SVG is refused.** It is the one image format that is also a document: an
  SVG can carry `<script>`, and this site serves media from its own origin, so
  a reader who follows a link straight to the file would execute it as
  same-origin JavaScript. `<img>` would not run it, but nothing stops a direct
  link. There is no safe-enough subset worth the risk here.
- **Pixels are bounded before decode.** A 200KB PNG can declare 40000x40000
  and cost gigabytes of RAM to decompress. The dimensions are checked from the
  header, before any pass over the data.
"""

import hashlib
import io
from dataclasses import dataclass

from django.conf import settings
from PIL import Image as PILImage
from PIL import ImageOps, UnidentifiedImageError

# Formats Pillow will open and we are willing to store. Deliberately a short
# allowlist rather than a denylist of SVG: Pillow can open a great many exotic
# formats, and "can be decoded" is not the same as "someone meant to upload it".
# HEIC is absent because reading it needs pillow-heif, which is a further
# dependency for a case iOS mostly handles itself — Safari converts to JPEG
# when a photo is chosen through a file input. The error message names it, so a
# macOS drag-and-drop gets an explanation rather than "unsupported".
ALLOWED_FORMATS = {"JPEG", "PNG", "GIF", "WEBP", "BMP", "TIFF"}

# Everything is stored as WebP: one decode path, one content type, one
# extension, and roughly a third off JPEG at the same visual quality — which
# matters more than usual when the bytes sit in database rows that get backed
# up and replicated.
OUTPUT_FORMAT = "WEBP"
OUTPUT_CONTENT_TYPE = "image/webp"


class ImageRejected(ValueError):
    """The upload cannot be stored, with a reason fit to show a person."""


@dataclass(frozen=True)
class NormalisedImage:
    data: bytes
    thumbnail: bytes
    width: int
    height: int
    checksum: str

    @property
    def byte_size(self):
        return len(self.data)


def _setting(name, default):
    return getattr(settings, name, default)


def normalise(upload):
    """An `UploadedFile` in, stored bytes out.

    Raises `ImageRejected` with a message meant for the person who chose the
    file. Everything that can be checked cheaply is checked before the pixels
    are touched.
    """
    max_bytes = _setting("CMS_MAX_UPLOAD_BYTES", 12 * 1024 * 1024)
    if upload.size > max_bytes:
        raise ImageRejected(
            f"That file is {_megabytes(upload.size)}MB. "
            f"The limit is {_megabytes(max_bytes)}MB — please resize it first."
        )

    # Anything that has already inspected this upload — a form field, a
    # middleware — leaves the pointer wherever it stopped, and a partial read
    # here would present as a corrupt image rather than as a bug.
    upload.seek(0)
    raw = upload.read()
    if not raw:
        raise ImageRejected("That file is empty.")

    # SVG is XML, so it never reaches Pillow's format detection as an image and
    # would otherwise fall out as a generic "not an image" — a confusing answer
    # for a file the uploader can see rendering in their browser. Name it.
    head = raw[:1024].lstrip()
    if head[:5] == b"<?xml" or b"<svg" in head[:200].lower():
        raise ImageRejected(
            "SVG images can carry scripts, so they cannot be uploaded. "
            "Please export a PNG or JPEG instead."
        )

    try:
        probe = PILImage.open(io.BytesIO(raw))
        image_format = probe.format
        width, height = probe.size
    except UnidentifiedImageError:
        raise ImageRejected(
            "That does not look like an image file. "
            "JPEG, PNG, GIF, WebP, BMP and TIFF can be uploaded; "
            "HEIC (the iPhone default on a Mac) cannot — export it as JPEG."
        ) from None
    except Exception as exc:  # pragma: no cover — Pillow's error surface is wide
        raise ImageRejected("That image could not be read.") from exc

    if image_format not in ALLOWED_FORMATS:
        raise ImageRejected(
            f"{image_format} images cannot be stored. "
            "Please upload a JPEG, PNG, GIF or WebP."
        )

    # Checked from the header, before anything decodes the data — this is the
    # decompression-bomb guard, and it is only a guard if it runs first.
    max_pixels = _setting("CMS_MAX_UPLOAD_PIXELS", 50_000_000)
    if width * height > max_pixels:
        raise ImageRejected(
            f"That image is {width}x{height}, which is larger than this site "
            "will process. Please resize it first."
        )

    try:
        source = PILImage.open(io.BytesIO(raw))
        source.load()
    except PILImage.DecompressionBombError:
        raise ImageRejected("That image is too large to process safely.") from None
    except Exception as exc:
        raise ImageRejected("That image could not be read.") from exc

    prepared = _strip_and_orient(source)

    full = prepared.copy()
    full.thumbnail(
        _square(_setting("CMS_IMAGE_MAX_DIMENSION", 1600)),
        PILImage.Resampling.LANCZOS,
    )

    thumb = prepared.copy()
    thumb.thumbnail(
        _square(_setting("CMS_THUMBNAIL_MAX_DIMENSION", 400)),
        PILImage.Resampling.LANCZOS,
    )

    data = _encode(full, _setting("CMS_IMAGE_QUALITY", 82))
    return NormalisedImage(
        data=data,
        thumbnail=_encode(thumb, _setting("CMS_THUMBNAIL_QUALITY", 70)),
        width=full.width,
        height=full.height,
        checksum=hashlib.sha256(data).hexdigest(),
    )


def _strip_and_orient(source):
    """Apply the EXIF rotation, then drop every trace of the metadata.

    Order matters: transposing reads the orientation tag, so stripping first
    would leave the image sideways with nothing left to say so.

    The strip is a `frombytes` round-trip rather than deleting keys, because
    metadata rides along in several places — `info`, an `exif` block, an ICC
    profile, XMP — and an allowlist of things to delete is exactly the kind of
    defence that silently stops covering a case when a library adds one. A new
    image built from raw pixels carries nothing by construction, and it stays
    in Pillow's C path, so it is not slow.
    """
    oriented = ImageOps.exif_transpose(source) or source

    # Do this before the round-trip: `frombytes` needs a mode whose bytes are
    # self-describing, which a palette ("P") image's are not.
    if oriented.mode in ("RGBA", "LA", "PA") or "transparency" in oriented.info:
        oriented = oriented.convert("RGBA")
    else:
        oriented = oriented.convert("RGB")

    return PILImage.frombytes(oriented.mode, oriented.size, oriented.tobytes())


def _encode(image, quality):
    buffer = io.BytesIO()
    image.save(buffer, format=OUTPUT_FORMAT, quality=quality, method=6)
    return buffer.getvalue()


def _square(dimension):
    """A bounding box for `thumbnail`, which only ever shrinks.

    `ImageOps.contain` would enlarge a small image to fill the box, turning a
    300px logo into a blurry 1600px one.
    """
    return (dimension, dimension)


def _megabytes(value):
    return round(value / (1024 * 1024), 1)
