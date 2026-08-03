from django.urls import path

from . import views

urlpatterns = [
    path("claim/", views.claim, name="claim"),
]
