from django.urls import path

from . import views

urlpatterns = [
    path("", views.home, name="home"),
    path("help/", views.help_page, name="help"),
]
