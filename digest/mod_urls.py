"""Moderator routes for the weekly email, mounted under `/moderate/`.

Apart from `urls.py` for the same reason as `content.mod_urls`: everything
here is decorated, everything there is public.
"""

from django.urls import path

from . import mod_views

urlpatterns = [
    path("digest/", mod_views.overview, name="mod_digest"),
    path("digest/switch/", mod_views.switch, name="mod_digest_switch"),
    path("digest/test/", mod_views.test_send, name="mod_digest_test"),
    path("digest/runs/<int:pk>/", mod_views.run_detail, name="mod_digest_run"),
    path("digest/runs/<int:pk>/email/", mod_views.run_email, name="mod_digest_run_email"),
    path("digest/subscribers/", mod_views.subscribers, name="mod_digest_subscribers"),
    path(
        "digest/subscribers/<int:pk>/",
        mod_views.subscriber_detail,
        name="mod_digest_subscriber",
    ),
]
