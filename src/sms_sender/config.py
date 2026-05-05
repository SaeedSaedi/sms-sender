"""Resolve API key from env / .env. Everything else flows through CLI flags."""
from __future__ import annotations

import os

from dotenv import load_dotenv

ENV_API_KEY = "KAVENEGAR_API_KEY"


def load_api_key() -> str:
    load_dotenv()  # picks up `.env` in cwd if present
    key = os.environ.get(ENV_API_KEY, "").strip()
    if not key:
        raise RuntimeError(
            f"{ENV_API_KEY} is not set. Put it in `.env` or export it before running."
        )
    return key
