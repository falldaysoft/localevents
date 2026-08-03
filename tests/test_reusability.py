"""Guards on the promise that this project is not tied to one town.

`localevents` is the reusable product; a live community site is one *instance*
of it. Instance identity belongs in environment variables, in the
admin-editable SiteConfig, or in an overlay under `instances/` — never in the
code, the templates, or the chart defaults.

These tests exist because that boundary erodes silently: someone hardcodes a
city into a default, a fixture, or a piece of help text, and nothing fails
until a second community tries to deploy it.
"""

from pathlib import Path

import pytest
from django.conf import settings

REPO_ROOT = Path(__file__).resolve().parent.parent

# Directories that are either not ours, generated, or deliberately
# instance-specific.
EXCLUDED_DIRS = {
    ".git",
    ".venv",
    "__pycache__",
    ".pytest_cache",
    "staticfiles",
    "node_modules",
    "instances",  # instance overlays are *supposed* to name a place
}

SCANNED_SUFFIXES = {".py", ".html", ".txt", ".cfg", ".ini", ".toml", ".yaml", ".yml"}

# Place names that must not appear in the product. Extend when a deployment
# adds a new one; the point is to fail loudly rather than to be exhaustive.
BANNED_SUBSTRINGS = ("brant",)


def iter_source_files():
    for path in REPO_ROOT.rglob("*"):
        if not path.is_file():
            continue
        if any(part in EXCLUDED_DIRS for part in path.parts):
            continue
        if path.suffix.lower() not in SCANNED_SUFFIXES:
            continue
        if path == Path(__file__):
            continue
        yield path


def test_no_town_identity_in_source():
    """No place name is baked into the product."""
    offenders = []
    for path in iter_source_files():
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        lowered = text.lower()
        for banned in BANNED_SUBSTRINGS:
            if banned in lowered:
                for lineno, line in enumerate(text.splitlines(), start=1):
                    if banned in line.lower():
                        rel = path.relative_to(REPO_ROOT)
                        offenders.append(f"{rel}:{lineno}: {line.strip()}")

    assert not offenders, (
        "Town-specific content found in the reusable product. Move it to an "
        "environment variable, to SiteConfig, or to an overlay under "
        "instances/:\n  " + "\n  ".join(offenders)
    )


def test_site_identity_settings_have_neutral_defaults():
    """A fresh checkout with no environment set must not claim to be anywhere."""
    for name in ("SITE_NAME", "SITE_TAGLINE", "CONTACT_EMAIL", "USER_AGENT"):
        value = getattr(settings, name).lower()
        for banned in BANNED_SUBSTRINGS:
            assert banned not in value, f"settings.{name} names a specific place"


@pytest.mark.parametrize(
    "name",
    ["SITE_NAME", "SITE_TAGLINE", "CONTACT_EMAIL", "MAP_CENTER_LAT", "MAP_CENTER_LNG",
     "MAP_ZOOM", "MAP_BBOX", "TILE_URL", "TILE_ATTRIBUTION", "USER_AGENT"],
)
def test_instance_settings_exist(name):
    """The identity knobs an instance needs to override are all present."""
    assert hasattr(settings, name), f"settings.{name} is missing"


def test_map_bbox_is_four_floats():
    assert len(settings.MAP_BBOX) == 4
    assert all(isinstance(v, float) for v in settings.MAP_BBOX)


@pytest.mark.django_db
def test_the_sites_row_is_not_djangos_default():
    """`django.contrib.sites` must name this instance, not example.com.

    allauth writes the Site row's name and domain into every confirmation and
    password-reset email. Django creates that row saying example.com and never
    touches it again, so without the post_migrate sync in core.apps every
    instance mails out someone else's identity. This asserts on the database
    the test run actually migrated, which is what proves the hook is wired up.
    """
    from django.contrib.sites.models import Site

    site = Site.objects.get(pk=settings.SITE_ID)
    assert site.domain != "example.com"
    assert site.name == settings.SITE_NAME


@pytest.mark.django_db
def test_the_sites_row_follows_the_environment(settings):
    """No place name is frozen into the row — it re-derives on every migrate."""
    from django.contrib.sites.models import Site

    from core.apps import sync_site

    settings.SITE_NAME = "Elsewhere Events"
    settings.SITE_BASE_URL = "https://elsewhere.example"
    sync_site(sender=None)

    site = Site.objects.get(pk=settings.SITE_ID)
    assert site.name == "Elsewhere Events"
    assert site.domain == "elsewhere.example"
