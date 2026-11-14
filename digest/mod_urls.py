"""Moderator routes for the weekly email, mounted under `/moderate/`.

Apart from `urls.py` for the same reason as `content.mod_urls`: everything
here is decorated, everything there is public.
"""

from django.urls import path

from . import mod_views

urlpatterns = [
    path("digest/", mod_views.overview, name="mod_digest"),
    path("digest/test/", mod_views.test_send, name="mod_digest_test"),
]
