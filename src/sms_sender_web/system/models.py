"""Settings for every campaign, that only an admin changes (plan 05): one
row. The frequency cap (decision 6: built, off until set), notification
targets, defaults for new campaigns, the hold on all sending and the credit
warning level; and Kavenegar's account as last checked."""
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
    # An admin's hold on all sending (the emergency stop): while it's set, no
    # send or test SMS runs, and none starts (jobs.services.hold_sending).
    sending_held_at = models.DateTimeField(null=True, blank=True)
    sending_held_by = models.ForeignKey(
        django_settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+",
    )
    # Warn (the campaign list, the notification targets) when Kavenegar's
    # credit is below this many rials. Empty: no warning.
    credit_floor = models.PositiveBigIntegerField(null=True, blank=True)

    @classmethod
    def load(cls) -> "SystemSettings":
        return cls.objects.get_or_create(pk=1)[0]

    @property
    def frequency_cap(self) -> FrequencyCap | None:
        if self.frequency_cap_sms and self.frequency_cap_days:
            return FrequencyCap(self.frequency_cap_sms, self.frequency_cap_days)
        return None


class ProviderCheck(models.Model):
    """Kavenegar's account as last asked, by the worker every 15 minutes and
    by the status page on every view (system/credit.py): one row."""
    credit = models.BigIntegerField(null=True, blank=True)      # rials
    problem = models.CharField(max_length=16, blank=True)       # no_key, refused or unreachable
    checked_at = models.DateTimeField(null=True, blank=True)
    # Under the admins' warning level since then; the targets hear once per drop.
    below_since = models.DateTimeField(null=True, blank=True)

    @classmethod
    def load(cls) -> "ProviderCheck":
        return cls.objects.get_or_create(pk=1)[0]
