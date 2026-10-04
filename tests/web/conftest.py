"""Fixtures for the dashboard tests. They need the `web` extra
(pip install -e ".[dev,web]"); without it, each test module skips itself."""
import pytest


@pytest.fixture
def operator(django_user_model):
    return django_user_model.objects.create_user(
        username="operator1", password="a-long-test-password-1",
    )


@pytest.fixture
def signed_in(client, operator):
    client.force_login(operator)
    return client
