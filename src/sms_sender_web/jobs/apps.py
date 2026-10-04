from django.apps import AppConfig


class JobsConfig(AppConfig):
    name = "sms_sender_web.jobs"
    label = "jobs"
    default_auto_field = "django.db.models.BigAutoField"
