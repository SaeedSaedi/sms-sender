"""The template library (plan 05, P2; Phase 4): a copy of each Kavenegar
template's text, to preview campaigns — the message with its tokens filled
in, its length and SMS parts. Kavenegar holds the template itself; sending
uses only its name."""
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
