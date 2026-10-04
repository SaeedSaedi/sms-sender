from django.apps import AppConfig


class SuppressionConfig(AppConfig):
    name = "sms_sender_web.suppression"
    label = "suppression"
    default_auto_field = "django.db.models.BigAutoField"
