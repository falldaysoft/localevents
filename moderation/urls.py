from django.urls import path

from . import views

urlpatterns = [
    path("", views.queue, name="mod_queue"),
    path("rising/", views.rising, name="mod_rising"),
    path("log/", views.audit, name="mod_audit"),
    path("<int:pk>/", views.submission_detail, name="mod_submission"),
    path("<int:pk>/assign/", views.assign, name="mod_assign"),
    path("<int:pk>/approve/", views.approve, name="mod_approve"),
    path("<int:pk>/reject/", views.reject, name="mod_reject"),
    path("<int:pk>/ask/", views.request_info, name="mod_request_info"),
    path("promote/<int:pk>/", views.promote, name="mod_promote"),
    # Namespaced under event/ so it never collides with the bare <int:pk>
    # submission route above, which it would otherwise shadow.
    path("event/<int:pk>/edit/", views.event_edit, name="mod_event_edit"),
]
