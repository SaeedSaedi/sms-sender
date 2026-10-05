from django.contrib.auth import views as auth_views
from django.urls import include, path

from . import views
from .audit import views as audit_views

urlpatterns = [
    path("healthz", views.healthz, name="healthz"),
    path("login/", auth_views.LoginView.as_view(redirect_authenticated_user=True), name="login"),
    path("logout/", auth_views.LogoutView.as_view(), name="logout"),
    path("activity/", audit_views.audit_log, name="audit_log"),
    path("", include("sms_sender_web.accounts.urls")),
    path("", include("sms_sender_web.campaigns.urls")),
    path("", include("sms_sender_web.reports.urls")),
    path("", include("sms_sender_web.segments.urls")),
    path("", include("sms_sender_web.suppression.urls")),
    path("", include("sms_sender_web.dashboard.urls")),
    path("", include("sms_sender_web.api.urls")),
    path("", include("sms_sender_web.system.urls")),
]
