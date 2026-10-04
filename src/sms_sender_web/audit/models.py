"""Who did what, and when (spec 4.12). Append-only: nothing in the app
edits or deletes an event."""
from django.conf import settings as django_settings
from django.db import models


class AuditEvent(models.Model):
    at = models.DateTimeField(auto_now_add=True, db_index=True)
    user = models.ForeignKey(
        django_settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
        related_name="+",
    )
    # Kept as text too, so the record survives the account.
    username = models.CharField(max_length=150, blank=True)
    action = models.CharField(max_length=64)
    campaign = models.CharField(max_length=64, blank=True)  # its slug, when there is one
    detail = models.JSONField(default=dict, blank=True)     # e.g. before / after
    ip = models.GenericIPAddressField(null=True, blank=True)

    class Meta:
        ordering = ["-at", "-id"]

    def __str__(self) -> str:
        return f"{self.at:%Y-%m-%d %H:%M} {self.username} {self.action}"
