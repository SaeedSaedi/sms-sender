from pathlib import Path

import pytest

from sms_sender.allowlist import ENV_ALLOWED_NUMBERS
from sms_sender.cli import LOG_FILE_ENV, TEST_NUMBER_ENV
from sms_sender.config import ENV_API_KEY
from sms_sender.shortlink import ENV_API_KEY as SHLINK_KEY_ENV
from sms_sender.shortlink import ENV_BASE_URL as SHLINK_BASE_ENV
from sms_sender.window import ENV_SEND_WINDOW

_DOTENV = Path(__file__).resolve().parents[1] / ".env"


def _dotenv_names() -> set[str]:
    """Names (never values) of the variables in the project's real `.env`."""
    if not _DOTENV.exists():
        return set()
    return {
        line.split("=", 1)[0].strip()
        for line in _DOTENV.read_text(encoding="utf-8").splitlines()
        if "=" in line and not line.lstrip().startswith("#")
    }


@pytest.fixture(autouse=True)
def _never_the_real_env(monkeypatch, tmp_path):
    """`load_dotenv` never overrides a variable that's already set, even to
    "", so blanking every name in the real `.env` (keys, the test number)
    keeps its values out of every test. A test that needs one sets its own."""
    # Restricted sending too (allowlist.py): off unless a test turns it on.
    for name in _dotenv_names() | {ENV_API_KEY, TEST_NUMBER_ENV, ENV_ALLOWED_NUMBERS}:
        monkeypatch.setenv(name, "")
    # No test may reach the real Shlink: a dummy key, and a host that
    # can't resolve (.invalid).
    monkeypatch.setenv(SHLINK_KEY_ENV, "test-key-not-real")
    monkeypatch.setenv(SHLINK_BASE_ENV, "https://shlink.invalid/u")
    # Tests mustn't depend on the time of day: the CLI's default sending
    # window (08:00–21:00 Tehran) is off unless a test sets one.
    monkeypatch.setenv(ENV_SEND_WINDOW, "off")
    # Never the real logs/: a CLI run in a test would rotate real history away.
    monkeypatch.setenv(LOG_FILE_ENV, str(tmp_path / "logs" / "sms-sender.log"))
