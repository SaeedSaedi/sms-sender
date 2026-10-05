"""Issuing, checking and revoking API tokens. A token is `smsk_` and 43
random URL-safe characters; only its SHA-256 digest is stored."""
from __future__ import annotations

import hashlib
import secrets

from django.utils import timezone

from .models import ApiToken

PREFIX = "smsk_"


def _digest(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()


def issue(name: str, user) -> tuple[ApiToken, str]:
    """A new token: the stored row, and the token itself (shown once)."""
    raw = PREFIX + secrets.token_urlsafe(32)
    token = ApiToken.objects.create(name=name, prefix=raw[:10], digest=_digest(raw), created_by=user)
    return token, raw


def authenticate(header: str) -> ApiToken | None:
    """The live token an `Authorization: Bearer …` header names, if any."""
    scheme, _, raw = (header or "").partition(" ")
    raw = raw.strip()
    if scheme.lower() != "bearer" or not raw.startswith(PREFIX):
        return None
    token = ApiToken.objects.filter(digest=_digest(raw), revoked_at__isnull=True).first()
    if token is not None:
        ApiToken.objects.filter(pk=token.pk).update(last_used_at=timezone.now())
    return token


def revoke(token: ApiToken) -> None:
    ApiToken.objects.filter(pk=token.pk, revoked_at__isnull=True).update(revoked_at=timezone.now())
