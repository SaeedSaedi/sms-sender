"""The Docker image installs the package, not the source tree. An app's
templates missing from package-data would break that app's pages in
Docker, and only there."""
from pathlib import Path

import sms_sender_web

ROOT = Path(sms_sender_web.__file__).parent
PYPROJECT = ROOT.parents[1] / "pyproject.toml"


def test_every_apps_templates_ship_in_the_package():
    listed = PYPROJECT.read_text(encoding="utf-8")
    folders = sorted(p.parent.name for p in ROOT.glob("*/templates") if p.is_dir())
    assert folders, "found no app templates — the check is broken"
    missing = [app for app in folders if f'"{app}/templates/**/*"' not in listed]
    assert missing == []
