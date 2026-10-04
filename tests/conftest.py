from pathlib import Path

import pytest

from sms_sender.cli import TEST_NUMBER_ENV
from sms_sender.config import ENV_API_KEY
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
def _never_the_real_env(monkeypatch):
    """`load_dotenv` never overrides a variable that's already set, even to
    "", so blanking every name in the real `.env` (keys, the test number)
    keeps its values out of every test. A test that needs one sets its own."""
    for name in _dotenv_names() | {ENV_API_KEY, TEST_NUMBER_ENV}:
        monkeypatch.setenv(name, "")
    # Tests mustn't depend on the time of day: the CLI's default sending
    # window (08:00–21:00 Tehran) is off unless a test sets one.
    monkeypatch.setenv(ENV_SEND_WINDOW, "off")
