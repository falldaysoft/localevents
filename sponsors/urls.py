from django.urls import path

from . import views

urlpatterns = [
    path("sponsor/", views.sponsor_info, name="sponsor_info"),
    path("sponsor/promote/<slug:slug>/", views.promote, name="sponsor_promote"),
    path("sponsor/orders/", views.my_orders, name="sponsor_orders"),
    path("sponsor/orders/<int:pk>/", views.order_detail, name="sponsor_order"),
    path("sponsor/orders/<int:pk>/return/", views.order_return, name="sponsor_order_return"),
    path("sponsor/orders/<int:pk>/withdraw/", views.order_withdraw, name="sponsor_order_withdraw"),
    path("sponsor/stripe/webhook/", views.webhook, name="sponsor_webhook"),
]
