from django.apps import AppConfig


class ApiConfig(AppConfig):
    name = "sms_sender_web.api"
    label = "api"
    default_auto_field = "django.db.models.BigAutoField"
