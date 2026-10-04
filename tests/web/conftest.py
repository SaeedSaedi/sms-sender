"""Fixtures for the dashboard tests. They need the `web` extra
(pip install -e ".[dev,web]"); without it, each test module skips itself."""
import pytest

PASSWORD = "a-long-test-password-1"


@pytest.fixture
def make_user(db, django_user_model):
    """make_user("name", "operator") → a user with that role (None: no role)."""
    from sms_sender_web.accounts.roles import set_role

    def make(username: str, role: str | None = "viewer", **fields):
        user = django_user_model.objects.create_user(username=username, password=PASSWORD, **fields)
        if role is not None:
            set_role(user, role)
        return user
    return make


@pytest.fixture
def viewer(make_user):
    return make_user("viewer1", "viewer")


@pytest.fixture
def signed_in(client, viewer):
    """A viewer: sees the campaigns, and has no second sign-in step."""
    client.force_login(viewer)
    return client


@pytest.fixture
def verified():
    """verified(client, user) → signed in and through the second step, with
    a linked authenticator app (returned too, for its codes)."""
    from django_otp import DEVICE_ID_SESSION_KEY
    from django_otp.plugins.otp_totp.models import TOTPDevice

    def sign_in(client, user):
        device = TOTPDevice.objects.create(user=user, name="authenticator", confirmed=True)
        client.force_login(user)
        session = client.session
        session[DEVICE_ID_SESSION_KEY] = device.persistent_id
        session.save()
        return device
    return sign_in
