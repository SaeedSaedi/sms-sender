"""The suppression list (spec 4.10): numbers that never get a campaign.
One global list, plus additions for a single campaign."""
from django.conf import settings as django_settings
from django.db import models
from django.db.models import Q


class Suppression(models.Model):
    phone = models.CharField(max_length=11)  # canonical 09XXXXXXXXX
    # Null: every campaign. Otherwise only this one.
    campaign = models.ForeignKey(
        "jobs.Campaign", null=True, blank=True, on_delete=models.CASCADE, related_name="suppressions",
    )
    note = models.CharField(max_length=200, blank=True)
    added_by = models.ForeignKey(
        django_settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
        related_name="+",
    )
    added_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-added_at", "-id"]
        indexes = [models.Index(fields=["phone"])]
        constraints = [
            # NULLs never collide in a UNIQUE index, so the global list needs its own.
            models.UniqueConstraint(
                fields=["phone"], condition=Q(campaign__isnull=True), name="suppression_global_once",
            ),
            models.UniqueConstraint(fields=["phone", "campaign"], name="suppression_campaign_once"),
        ]

    def __str__(self) -> str:
        return f"{self.phone} ({self.campaign_id or 'all'})"
