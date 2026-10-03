import pytest

from sms_sender.window import ENV_SEND_WINDOW


@pytest.fixture(autouse=True)
def _no_sending_window_unless_asked(monkeypatch):
    """Tests mustn't depend on the time of day: the CLI's default sending
    window (08:00–21:00 Tehran) is off unless a test sets one."""
    monkeypatch.setenv(ENV_SEND_WINDOW, "off")
