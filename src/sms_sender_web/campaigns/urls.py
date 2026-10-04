from django.urls import path

from . import views

urlpatterns = [
    path("campaigns/new/", views.campaign_new, name="campaign_new"),
    path("campaigns/<slug:slug>/", views.campaign_detail, name="campaign_detail"),
    path("campaigns/<slug:slug>/settings/", views.campaign_settings, name="campaign_settings"),
    path("campaigns/<slug:slug>/check/", views.campaign_check, name="campaign_check"),
    path("campaigns/<slug:slug>/live/", views.campaign_live, name="campaign_live"),
    path("campaigns/<slug:slug>/<str:action>/", views.campaign_action, name="campaign_action"),
]
