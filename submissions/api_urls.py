from django.urls import path

from . import api

urlpatterns = [
    path("", api.index, name="api_index"),
    path("categories/", api.categories, name="api_categories"),
    path("extract/", api.extract, name="api_extract"),
    path("submissions/", api.submissions, name="api_submissions"),
    path("submissions/<int:pk>/", api.submission_detail, name="api_submission"),
]
