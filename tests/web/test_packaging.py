"""The Docker image installs the package, not the source tree. An app's
templates missing from package-data would break that app's pages in
Docker, and only there."""
from pathlib import Path

import sms_sender_web

ROOT = Path(sms_sender_web.__file__).parent
PYPROJECT = ROOT.parents[1] / "pyproject.toml"
DOCKERFILE = ROOT.parents[1] / "Dockerfile"


def test_every_apps_templates_ship_in_the_package():
    listed = PYPROJECT.read_text(encoding="utf-8")
    folders = sorted(p.parent.name for p in ROOT.glob("*/templates") if p.is_dir())
    assert folders, "found no app templates — the check is broken"
    missing = [app for app in folders if f'"{app}/templates/**/*"' not in listed]
    assert missing == []


def test_the_image_serves_with_threads():
    """Browsers reach gunicorn directly and open idle connections ahead of
    time. A sync worker waits on one until it's killed, and a page gets
    gunicorn's "Internal Server Error" (seen in the real run, 2026-10-05)."""
    command = next(line for line in DOCKERFILE.read_text(encoding="utf-8").splitlines() if line.startswith("CMD"))
    assert "--worker-class gthread" in command and "--threads" in command
