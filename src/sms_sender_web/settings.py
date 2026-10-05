"""Settings for the Persian campaign dashboard (spec 4.9, 4.11, 4.12).

Everything that differs between machines comes from the environment: the
local `.env` (loaded by manage.py and wsgi.py, never here, so tests can't
pick up real values) or the deployment's secrets.
"""
from __future__ import annotations

import os
from pathlib import Path

from django.core.exceptions import ImproperlyConfigured


def _bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name, "").strip().lower()
    return default if not raw else raw in ("1", "true", "yes", "on")


def _list(name: str, default: str = "") -> list[str]:
    return [part.strip() for part in os.environ.get(name, default).split(",") if part.strip()]


BASE_DIR = Path(__file__).resolve().parent
# Sandbox mode (step 3.7): Kavenegar and Shlink are simulated and nothing is
# sent. Everything, the app DB included, lives in data/sandbox/, so sandbox
# runs never mix with real campaigns. See jobs/sandbox.py.
SANDBOX = _bool("SMS_SENDER_SANDBOX")
# The app DB and the campaign DBs (data/db/<campaign>.db) live here.
DATA_DIR = Path(os.environ.get("SMS_SENDER_DATA_DIR", "data")).resolve()
if SANDBOX:
    DATA_DIR = DATA_DIR / "sandbox"
SMS_SENDER_DB_DIR = DATA_DIR / "db"
# SQLite can't create a DB in a folder that doesn't exist yet (the first
# start in sandbox mode, or a fresh install): the app DB, and every
# campaign's. Only these two; the CLI's own --state paths are never created.
SMS_SENDER_DB_DIR.mkdir(parents=True, exist_ok=True)
# `manage.py backup` writes here. Keep it off the data volume's disk, or
# copy it elsewhere (encrypted): it holds phone numbers (docs/deploy.md).
BACKUP_DIR = Path(os.environ.get("SMS_SENDER_BACKUP_DIR") or DATA_DIR / "backups").resolve()

SECRET_KEY = os.environ.get("DJANGO_SECRET_KEY", "")
if not SECRET_KEY:
    raise ImproperlyConfigured(
        "DJANGO_SECRET_KEY is not set: put a long random value in .env "
        '(python -c "import secrets; print(secrets.token_urlsafe(50))")'
    )
DEBUG = _bool("DJANGO_DEBUG")
# Internal tool, reached over NetBird (spec 4.12): list the host names or IPs
# people use, e.g. the machine's NetBird IP.
ALLOWED_HOSTS = _list("DJANGO_ALLOWED_HOSTS", "127.0.0.1,localhost")
CSRF_TRUSTED_ORIGINS = _list("DJANGO_CSRF_TRUSTED_ORIGINS")

INSTALLED_APPS = [
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "django_otp",
    "django_otp.plugins.otp_totp",
    "sms_sender_web.accounts",
    "sms_sender_web.audit",
    "sms_sender_web.dashboard",
    "sms_sender_web.jobs",
    "sms_sender_web.segments",
    "sms_sender_web.suppression",
    "sms_sender_web.campaigns",
    "sms_sender_web.reports",
    "sms_sender_web.api",
    "sms_sender_web.system",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    # Every page needs a login, except the ones marked @login_not_required.
    # (Django's, with live updates sending the whole page to the login.)
    "sms_sender_web.accounts.middleware.LoginRequired",
    # Operators and admins also confirm a code from their authenticator app
    # (spec 4.12); until they do, every page leads to that step.
    "django_otp.middleware.OTPMiddleware",
    "sms_sender_web.accounts.middleware.TwoFactorMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "sms_sender_web.urls"
WSGI_APPLICATION = "sms_sender_web.wsgi.application"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
                "sms_sender_web.views.sandbox",
                "sms_sender_web.views.navigation",
            ],
        },
    },
]

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": DATA_DIR / "app.db",
        # The web app and the worker write to the same file: WAL, and take the
        # write lock up front instead of failing halfway through a transaction.
        "OPTIONS": {
            "init_command": "PRAGMA journal_mode=WAL; PRAGMA synchronous=NORMAL;",
            "transaction_mode": "IMMEDIATE",
            "timeout": 30,
        },
    }
}

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator",
     "OPTIONS": {"min_length": 12}},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]
LOGIN_URL = "login"
LOGIN_REDIRECT_URL = "home"
LOGOUT_REDIRECT_URL = "login"
# Shown in the authenticator app next to the account.
OTP_TOTP_ISSUER = "Kifpool SMS"

# Entirely Persian (spec 4.11): one language, right to left, Tehran time.
# Dates are shown in the Solar Hijri calendar by the `jalali` filter; they're
# stored in UTC.
LANGUAGE_CODE = "fa"
LANGUAGES = [("fa", "فارسی")]
LOCALE_PATHS = [BASE_DIR / "locale"]
USE_I18N = True
TIME_ZONE = "Asia/Tehran"
USE_TZ = True

STATIC_URL = "static/"
STATICFILES_DIRS = [BASE_DIR / "static"]
STATIC_ROOT = Path(os.environ.get("DJANGO_STATIC_ROOT", "build/static")).resolve()
STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": "whitenoise.storage.CompressedManifestStaticFilesStorage"},
}

# Locally the traffic is plain HTTP inside NetBird's WireGuard tunnel; behind
# TLS in production, set DJANGO_SECURE_COOKIES=1.
SESSION_COOKIE_SECURE = CSRF_COOKIE_SECURE = _bool("DJANGO_SECURE_COOKIES")
# Behind a TLS proxy that sets X-Forwarded-Proto (and drops any value a
# client sent), DJANGO_TRUST_PROXY_SSL=1 lets Django see HTTPS requests as
# secure. Never set it when clients reach gunicorn directly.
if _bool("DJANGO_TRUST_PROXY_SSL"):
    SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
SESSION_COOKIE_AGE = 8 * 3600
SESSION_COOKIE_HTTPONLY = True
SECURE_CONTENT_TYPE_NOSNIFF = True
SECURE_REFERRER_POLICY = "same-origin"
X_FRAME_OPTIONS = "DENY"

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# The engine's and the worker's logs, as key=value lines on the console
# (docker logs), like the CLI's.
LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {"kv": {"()": "sms_sender.logging_config.KeyValueFormatter"}},
    "handlers": {"console": {"class": "logging.StreamHandler", "formatter": "kv"}},
    "loggers": {
        "sms_sender": {"handlers": ["console"], "level": "INFO"},
        "sms_sender_web": {"handlers": ["console"], "level": "INFO"},
    },
}
