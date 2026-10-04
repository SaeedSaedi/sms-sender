from django.urls import path

from . import views

urlpatterns = [
    path("suppression/", views.suppression_list, name="suppression"),
]
