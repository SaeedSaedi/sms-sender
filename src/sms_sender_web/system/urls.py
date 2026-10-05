from django.urls import path

from . import views

urlpatterns = [
    path("system/", views.system_settings, name="system_settings"),
    path("system/backups/", views.backups, name="backups"),
    path("system/hold/", views.sending_hold, name="sending_hold"),
]
