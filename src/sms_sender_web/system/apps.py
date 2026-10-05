from django.apps import AppConfig


class SystemConfig(AppConfig):
    name = "sms_sender_web.system"
    label = "system"
    default_auto_field = "django.db.models.BigAutoField"
