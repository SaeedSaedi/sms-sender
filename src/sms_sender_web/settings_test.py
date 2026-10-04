"""Settings for the test suite: a throwaway secret and data directory, and
plain static files (no collectstatic manifest needed)."""
import os
import tempfile

os.environ.setdefault("DJANGO_SECRET_KEY", "test-only-not-a-secret")
os.environ.setdefault("SMS_SENDER_DATA_DIR", tempfile.mkdtemp(prefix="sms-sender-web-test-"))

from .settings import *  # noqa: E402,F401,F403

STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
}
# Fast hashing: tests create users.
PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]
# WhiteNoise looks for the collected files at startup; tests never collect.
STATIC_ROOT = DATA_DIR / "static"  # noqa: F405
STATIC_ROOT.mkdir(parents=True, exist_ok=True)
# A file, not SQLite's shared in-memory DB: the worker's heartbeat thread
# writes alongside the test's own connection, and shared-cache table locks
# fail at once instead of waiting.
DATABASES["default"]["TEST"] = {"NAME": str(DATA_DIR / "test-app.db")}  # noqa: F405
