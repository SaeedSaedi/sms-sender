"""Settings for every campaign, that only an admin changes (plan 05): one
row. So far the frequency cap (decision 6: built, off until set)."""
from django.conf import settings as django_settings
from django.db import models

from sms_sender.frequency import FrequencyCap


class SystemSettings(models.Model):
    # At most `frequency_cap_sms` SMS to a number in `frequency_cap_days`
    # days, across every campaign. Either empty: no cap.
    frequency_cap_sms = models.PositiveSmallIntegerField(null=True, blank=True)
    frequency_cap_days = models.PositiveSmallIntegerField(null=True, blank=True)
    # Where a send's end is announced (the CLI's --notify): slack:<webhook>,
    # telegram:<bot token>:<chat id>, or an https:// webhook. Secrets: the
    # pages show them masked (notify.redact_target).
    notify_targets = models.JSONField(default=list, blank=True)
    # What a new campaign starts with; empty: the built-in default.
    default_send_window = models.CharField(max_length=11, blank=True, default="")
    default_rate = models.CharField(max_length=16, blank=True, default="")
    updated_by = models.ForeignKey(
        django_settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+",
    )
    updated_at = models.DateTimeField(auto_now=True)

    @classmethod
    def load(cls) -> "SystemSettings":
        return cls.objects.get_or_create(pk=1)[0]

    @property
    def frequency_cap(self) -> FrequencyCap | None:
        if self.frequency_cap_sms and self.frequency_cap_days:
            return FrequencyCap(self.frequency_cap_sms, self.frequency_cap_days)
        return None
