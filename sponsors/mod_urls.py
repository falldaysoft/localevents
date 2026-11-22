"""Moderator routes for sponsorships, mounted under `/moderate/`."""

from django.urls import path

from . import mod_views

urlpatterns = [
    path("sponsors/", mod_views.queue, name="mod_sponsors"),
    path("sponsors/<int:pk>/", mod_views.detail, name="mod_sponsor"),
    path("sponsors/<int:pk>/approve/", mod_views.approve, name="mod_sponsor_approve"),
    path("sponsors/<int:pk>/reject/", mod_views.reject, name="mod_sponsor_reject"),
]
