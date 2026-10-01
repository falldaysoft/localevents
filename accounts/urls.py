from django.urls import path

from . import views

urlpatterns = [
    path("claim/", views.claim, name="claim"),
    path("profile/", views.profile, name="profile"),
    path("profile/tokens/", views.create_api_token, name="api_token_create"),
    path(
        "profile/tokens/<int:pk>/revoke/",
        views.revoke_api_token,
        name="api_token_revoke",
    ),
]
