from django.apps import AppConfig


class ReportsConfig(AppConfig):
    """Reports per campaign, segment and recipient, downloads, and the
    status page (spec 4.9). Read only: nothing here sends."""

    name = "sms_sender_web.reports"
    label = "reports"
