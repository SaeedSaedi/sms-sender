from django.contrib.auth import views as auth_views
from django.urls import include, path

from . import views
from .accounts import views as account_views
from .audit import views as audit_views

urlpatterns = [
    path("healthz", views.healthz, name="healthz"),
    path("login/", account_views.SignIn.as_view(), name="login"),
    path("logout/", auth_views.LogoutView.as_view(), name="logout"),
    path("activity/", audit_views.audit_log, name="audit_log"),
    path("activity/export.csv", audit_views.audit_csv, name="audit_csv"),
    path("", include("sms_sender_web.accounts.urls")),
    path("", include("sms_sender_web.campaigns.urls")),
    path("", include("sms_sender_web.reports.urls")),
    path("", include("sms_sender_web.segments.urls")),
    path("", include("sms_sender_web.suppression.urls")),
    path("", include("sms_sender_web.dashboard.urls")),
    path("", include("sms_sender_web.api.urls")),
    path("", include("sms_sender_web.system.urls")),
]
