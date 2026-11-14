from django.urls import path

from . import views

urlpatterns = [
    path("digest/", views.signup, name="digest_signup"),
    path("digest/sent/", views.sent, name="digest_sent"),
    path("digest/confirm/<str:key>/", views.confirm, name="digest_confirm"),
    path(
        "digest/unsubscribe/<str:token>/", views.unsubscribe, name="digest_unsubscribe"
    ),
    path("profile/digest/", views.profile_toggle, name="digest_profile_toggle"),
]
