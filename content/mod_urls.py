"""Moderator routes, mounted under `/moderate/` beside the queue.

A separate module from `urls.py` so that the prefix and the access rule line
up exactly: everything reachable from here is decorated, everything in
`urls.py` is public, and neither file needs reading twice to be sure.
"""

from django.urls import path

from . import views

urlpatterns = [
    path("pages/", views.pages, name="mod_pages"),
    path("pages/new/", views.page_new, name="mod_page_new"),
    path("pages/<int:pk>/", views.page_edit, name="mod_page_edit"),
    path("pages/<int:pk>/delete/", views.page_delete, name="mod_page_delete"),
    path("media/", views.media, name="mod_media"),
    path("media/<int:pk>/", views.image_edit, name="mod_image_edit"),
    path("media/<int:pk>/delete/", views.image_delete, name="mod_image_delete"),
]
