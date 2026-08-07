"""Public routes: reading a page, and fetching an image.

Pages sit under `/p/` rather than at the site root. A bare `/<slug>/` would be
prettier and would also put moderator-chosen text into the same namespace as
every route this project might add later — one page called "submit" or "events"
and something stops working, with no error to say why. A prefix costs two
characters and removes the whole class of problem.
"""

from django.urls import path

from . import views

urlpatterns = [
    path("p/<slug:slug>/", views.page_detail, name="page_detail"),
    # Before the general route below, which would otherwise swallow it.
    path("media/<int:pk>/thumb.webp", views.image_thumbnail, name="image_thumbnail"),
    path("media/<int:pk>/<str:filename>", views.image_file, name="image_file"),
]
