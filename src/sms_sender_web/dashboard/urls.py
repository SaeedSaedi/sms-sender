from django.urls import path

from . import views

urlpatterns = [
    path("", views.home, name="home"),
    path("home/sends/", views.home_sends, name="home_sends"),
    path("campaigns/", views.campaign_list, name="campaign_list"),
    path("help/", views.help_page, name="help"),
]
