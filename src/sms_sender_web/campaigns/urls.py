from django.urls import path

from . import library, views

urlpatterns = [
    path("campaigns/new/", views.campaign_new, name="campaign_new"),
    path("campaigns/<slug:slug>/", views.campaign_detail, name="campaign_detail"),
    path("campaigns/<slug:slug>/settings/", views.campaign_settings, name="campaign_settings"),
    path("campaigns/<slug:slug>/settings/preview/", views.settings_preview, name="campaign_settings_preview"),
    path("campaigns/<slug:slug>/check/", views.campaign_check, name="campaign_check"),
    path("campaigns/<slug:slug>/preview/", views.campaign_preview, name="campaign_preview"),
    path("campaigns/<slug:slug>/live/", views.campaign_live, name="campaign_live"),
    path("campaigns/<slug:slug>/<str:action>/", views.campaign_action, name="campaign_action"),
    path("templates/", library.template_list, name="template_list"),
    path("templates/new/", library.template_edit, name="template_new"),
    path("templates/<int:pk>/", library.template_edit, name="template_edit"),
    path("templates/<int:pk>/delete/", library.template_delete, name="template_delete"),
]
