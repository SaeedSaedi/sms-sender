from django.apps import AppConfig


class AuditConfig(AppConfig):
    name = "sms_sender_web.audit"
    label = "audit"
    default_auto_field = "django.db.models.BigAutoField"

    def ready(self) -> None:
        from django.contrib.auth.signals import (
            user_logged_in,
            user_logged_out,
            user_login_failed,
        )

        from .record import record

        def logged_in(sender, request, user, **kwargs):
            from ..accounts.views import remembered

            if request is not None and remembered(request):
                record("login", request=request, user=user, remembered=True)  # for 30 days (D5)
            else:
                record("login", request=request, user=user)

        def logged_out(sender, request, user, **kwargs):
            if user is not None:
                record("logout", request=request, user=user)

        def login_failed(sender, credentials, request=None, **kwargs):
            # The username tried, never the password.
            record("login_failed", request=request, username=credentials.get("username", ""))

        user_logged_in.connect(logged_in, weak=False, dispatch_uid="audit_login")
        user_logged_out.connect(logged_out, weak=False, dispatch_uid="audit_logout")
        user_login_failed.connect(login_failed, weak=False, dispatch_uid="audit_login_failed")
