from django.urls import path

from . import views

urlpatterns = [
    path("segments/", views.segment_list, name="segment_list"),
    path("segments/upload/", views.segment_upload, name="segment_upload"),
    path("segments/<slug:slug>/", views.segment_detail, name="segment_detail"),
    path("segments/<slug:slug>/columns/", views.segment_map, name="segment_map"),
    path("segments/<slug:slug>/delete/", views.segment_delete, name="segment_delete"),
    path("segments/<slug:slug>/download/", views.segment_download, name="segment_download"),
    path("segments/<slug:slug>/replace/", views.segment_replace, name="segment_replace"),
]
