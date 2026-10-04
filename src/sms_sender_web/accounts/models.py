"""What the dashboard keeps about a person beyond Django's user."""
from django.conf import settings as django_settings
from django.db import models


class Profile(models.Model):
    user = models.OneToOneField(
        django_settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="profile",
    )
    # The operator's own mobile number: test SMS go here, and nowhere else.
    test_phone = models.CharField(max_length=11, blank=True)

    def __str__(self) -> str:
        return f"profile of {self.user_id}"


def test_phone_of(user) -> str:
    """The user's own number for test SMS, or "" when they haven't set one."""
    profile = Profile.objects.filter(user=user).first() if getattr(user, "pk", None) else None
    return profile.test_phone if profile else ""
