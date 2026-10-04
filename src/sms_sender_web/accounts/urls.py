from django.urls import path

from . import views

urlpatterns = [
    path("2fa/", views.two_factor, name="two_factor"),
    path("2fa/setup/", views.two_factor_setup, name="two_factor_setup"),
    path("users/", views.users, name="users"),
    path("password/", views.PasswordChange.as_view(), name="password_change"),
]
