from django.contrib import admin
from django.urls import include, path

urlpatterns = [
    path("admin/", admin.site.urls),
    path("accounts/", include("allauth.urls")),
    path("", include("accounts.urls")),
    path("moderate/", include("moderation.urls")),
    path("", include("submissions.urls")),
    path("", include("web.urls")),
]
