"""The template library (plan 05, P2; Phase 4): a copy of each Kavenegar
template's text, to preview campaigns — the message with its tokens filled
in, its length and SMS parts. Kavenegar holds the template itself; sending
uses only its name.

Presets (plan 06, L3): a kind of message sent again and again, kept with
what fills its tokens, so a new alert only needs today's values."""
from django.conf import settings as django_settings
from django.db import models


class MessageTemplate(models.Model):
    # Exactly as in the Kavenegar panel: the name a campaign sends with.
    name = models.CharField(max_length=100, unique=True)
    # The template's text, with %token, %token2, %token3, %token10, %token20.
    text = models.TextField()
    note = models.CharField(max_length=200, blank=True)
    updated_by = models.ForeignKey(
        django_settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
        related_name="+",
    )
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["name"]

    def __str__(self) -> str:
        return self.name


class Preset(models.Model):
    """A message sent again and again (a daily price alert): its template,
    what fills each token, its default segments and its sending settings,
    in `Campaign.settings`' terms. Each alert made from it is a campaign of
    its own (`Campaign.preset`): its series."""

    # New alerts' short names start with it: coin-price → coin-price-1405-07-14.
    slug = models.SlugField(max_length=40, unique=True)
    name = models.CharField(max_length=120)
    # A new alert's name: {name} and {date} (today, Solar Hijri) are filled in.
    name_pattern = models.CharField(max_length=160, blank=True)
    # As a campaign's: template, tokens (their defaults), token_columns,
    # value_maps, links, segment and more_segments (the defaults), the window,
    # rate, workers and advanced settings.
    settings = models.JSONField(default=dict, blank=True)
    # A label for each token typed per alert, e.g. {"token": "نام کوین"}.
    labels = models.JSONField(default=dict, blank=True)
    # The values the latest alert went out with, suggested for the next one.
    last_values = models.JSONField(default=dict, blank=True)
    created_by = models.ForeignKey(
        django_settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
        related_name="+",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    # Put away: no new alerts from it; its series stays.
    archived_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["name"]

    def __str__(self) -> str:
        return self.slug

