"""Tokens for the attribution API (plan 05, decision 8). An admin issues
one per system that reads it; the token is shown once and only its digest
is kept, so a copy of the database can't call the API."""
from django.conf import settings as django_settings
from django.db import models


class ApiToken(models.Model):
    name = models.CharField(max_length=100)       # which system uses it
    prefix = models.CharField(max_length=16)      # its first characters, to tell tokens apart
    digest = models.CharField(max_length=64, unique=True)  # SHA-256 of the whole token
    created_by = models.ForeignKey(
        django_settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    last_used_at = models.DateTimeField(null=True, blank=True)
    revoked_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at", "-id"]

    def __str__(self) -> str:
        return f"{self.name} ({self.prefix}…)"
