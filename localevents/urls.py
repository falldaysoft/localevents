from django.contrib import admin
from django.urls import include, path

urlpatterns = [
    path("admin/", admin.site.urls),
    path("accounts/", include("allauth.urls")),
    path("", include("accounts.urls")),
    path("moderate/", include("moderation.urls")),
    # Also under moderate/, and safe beside it: moderation's only greedy route
    # is <int:pk>, which cannot match "pages" or "media".
    path("moderate/", include("content.mod_urls")),
    path("", include("submissions.urls")),
    path("", include("content.urls")),
    path("", include("web.urls")),
]
