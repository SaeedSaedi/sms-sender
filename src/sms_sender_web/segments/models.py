"""Segments: recipient lists uploaded through the dashboard (spec 4.9).
The file itself is personal data and lives in data/segments/, never in
the app DB; the row holds its mapping and a summary."""
from pathlib import Path

from django.conf import settings as django_settings
from django.db import models
from django.utils.translation import gettext_lazy as _


class Segment(models.Model):
    class Status(models.TextChoices):
        DRAFT = "draft", _("Columns not chosen yet")
        READY = "ready", _("Ready")

    # Lowercase letters, digits and dashes: it's recorded per recipient and
    # used in Shlink tags and UTM values, like the CLI's --segment.
    slug = models.SlugField(max_length=64, unique=True)
    name = models.CharField(max_length=200)
    original_name = models.CharField(max_length=255, blank=True)
    status = models.CharField(max_length=8, choices=Status.choices, default=Status.DRAFT)
    has_header = models.BooleanField(default=True)
    # The prepared file's header: `phone`, then the user-ID column (if any)
    # and the columns kept for tokens.
    columns = models.JSONField(default=list, blank=True)
    user_id_column = models.CharField(max_length=200, blank=True)
    token_columns = models.JSONField(default=list, blank=True)
    summary = models.JSONField(default=dict, blank=True)
    uploaded_by = models.ForeignKey(
        django_settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
        related_name="+",
    )
    uploaded_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-uploaded_at", "-id"]

    def __str__(self) -> str:
        return self.slug

    @staticmethod
    def folder() -> Path:
        return Path(django_settings.DATA_DIR) / "segments"

    @property
    def path(self) -> Path:
        """The prepared copy the engine reads."""
        return self.folder() / f"{self.slug}.csv"

    @property
    def upload_path(self) -> Path:
        """The file as uploaded, kept only until its columns are chosen."""
        return self.folder() / f"{self.slug}.upload"

    def delete_files(self) -> None:
        for path in (self.path, self.upload_path):
            path.unlink(missing_ok=True)
