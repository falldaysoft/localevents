from django.urls import path

from . import views

urlpatterns = [
    path("", views.index, name="index"),
    path("events/<slug:slug>/", views.event_detail, name="event_detail"),
    path("events.geojson", views.events_geojson, name="events_geojson"),
    path("healthz", views.healthz, name="healthz"),
]
