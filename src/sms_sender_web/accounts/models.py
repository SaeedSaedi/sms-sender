"""What the dashboard keeps about a person beyond Django's user."""
from django.conf import settings as django_settings
from django.db import models


class Profile(models.Model):
    user = models.OneToOneField(
        django_settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="profile",
    )
    # The operator's own mobile number: test SMS go here, and nowhere else.
    test_phone = models.CharField(max_length=11, blank=True)
    # Set by an admin (a new account, or a reset password): until they choose
    # their own, every page leads to the password change.
    must_change_password = models.BooleanField(default=False)
    # When they last opened the notifications: newer ones count as new.
    notifications_seen_at = models.DateTimeField(null=True, blank=True)

    def __str__(self) -> str:
        return f"profile of {self.user_id}"


def test_phone_of(user) -> str:
    """The user's own number for test SMS, or "" when they haven't set one."""
    profile = Profile.objects.filter(user=user).first() if getattr(user, "pk", None) else None
    return profile.test_phone if profile else ""


def must_change_password(user) -> bool:
    """Looked up once per user object, like the role."""
    try:
        return user._must_change_password
    except AttributeError:
        pass
    profile = Profile.objects.filter(user=user).first() if getattr(user, "pk", None) else None
    user._must_change_password = bool(profile and profile.must_change_password)
    return user._must_change_password


def ask_to_change_password(user, ask: bool = True) -> None:
    Profile.objects.update_or_create(user=user, defaults={"must_change_password": ask})
    try:
        del user._must_change_password
    except AttributeError:
        pass
