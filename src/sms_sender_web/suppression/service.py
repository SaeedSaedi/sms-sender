"""Reading and changing the suppression list from anywhere: the pages, and
the engine that builds each send run."""
from __future__ import annotations

from typing import Iterable

from django.db import transaction
from django.db.models import Q

from .models import Suppression

_CHUNK = 900


def phones_for(campaign=None) -> frozenset[str]:
    """Every number this campaign must never send to: the global list, plus
    its own additions. Without a campaign, the global list."""
    scope = Q(campaign__isnull=True)
    if campaign is not None:
        scope |= Q(campaign=campaign)
    return frozenset(Suppression.objects.filter(scope).values_list("phone", flat=True))


def add(phones: Iterable[str], *, campaign=None, note: str = "", user=None) -> int:
    """Add canonical numbers; ones already listed are left alone. Returns
    how many were new."""
    wanted = sorted(set(phones))
    with transaction.atomic():
        listed: set[str] = set()
        for i in range(0, len(wanted), _CHUNK):  # SQLite caps the parameters per query
            listed.update(
                Suppression.objects.filter(phone__in=wanted[i:i + _CHUNK], campaign=campaign)
                .values_list("phone", flat=True)
            )
        new = [p for p in wanted if p not in listed]
        Suppression.objects.bulk_create(
            [Suppression(phone=p, campaign=campaign, note=note, added_by=user) for p in new],
            batch_size=500,
        )
    return len(new)
