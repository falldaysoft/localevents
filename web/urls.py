from django.contrib.sitemaps.views import sitemap
from django.urls import path

from . import views
from .sitemaps import SITEMAPS

urlpatterns = [
    path("", views.index, name="index"),
    path("events/<slug:slug>/", views.event_detail, name="event_detail"),
    path("events.geojson", views.events_geojson, name="events_geojson"),
    path("venues/", views.venue_list, name="venue_list"),
    path("venues/<slug:slug>/", views.venue_detail, name="venue_detail"),
    path("categories/", views.category_list, name="category_list"),
    path("categories/<slug:slug>/", views.category_detail, name="category_detail"),
    path("robots.txt", views.robots_txt, name="robots_txt"),
    path("sitemap.xml", sitemap, {"sitemaps": SITEMAPS}, name="sitemap"),
    path("healthz", views.healthz, name="healthz"),
]
