from django.urls import path

from . import views

urlpatterns = [
    path("submit/", views.start, name="submit"),
    path("submit/mine/", views.my_submissions, name="my_submissions"),
    path("submit/<int:pk>/", views.submission_detail, name="submission_detail"),
    path(
        "submit/<int:pk>/status/",
        views.submission_status_fragment,
        name="submission_status",
    ),
]
