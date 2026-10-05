from django.urls import path

from . import views

urlpatterns = [
    path("status/", views.status, name="status"),
    path("analytics/", views.analytics, name="analytics"),
    path("reports/<slug:slug>/", views.report, name="report"),
    path("reports/<slug:slug>/reveal/", views.reveal, name="report_reveal"),
    path("reports/<slug:slug>/summary.csv", views.summary_csv, name="report_summary_csv"),
    path("reports/<slug:slug>/attribution.csv", views.attribution_csv, name="report_attribution_csv"),
    path("reports/<slug:slug>/clickers.csv", views.clickers_csv, name="report_clickers_csv"),
    path("reports/<slug:slug>/recipients.csv", views.recipients_csv, name="report_recipients_csv"),
    path("reports/<slug:slug>/failed.csv", views.failed_csv, name="report_failed_csv"),
]
