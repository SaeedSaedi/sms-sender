"""Profile loader + CLI integration tests."""
from __future__ import annotations

from pathlib import Path

import pytest
from click.testing import CliRunner

from sms_sender.cli import cli
from sms_sender.profile import ProfileError, load_profile, to_default_map


# ---------- load_profile (unit) ----------


def _write(tmp_path: Path, body: str) -> Path:
    p = tmp_path / "sms-sender.toml"
    p.write_text(body, encoding="utf-8")
    return p


def test_load_profile_returns_default_section(tmp_path):
    cfg = _write(tmp_path, """
        [profile.default]
        workers = 7
        rate = "5/s"
    """)
    assert load_profile(cfg, None) == {"workers": 7, "rate": "5/s"}


def test_load_profile_named_overrides_default(tmp_path):
    cfg = _write(tmp_path, """
        [profile.default]
        workers = 3
        template = "fallback"

        [profile.verify]
        template = "verify-tpl"
        token = "12345"
    """)
    merged = load_profile(cfg, "verify")
    assert merged == {"workers": 3, "template": "verify-tpl", "token": "12345"}


def test_load_profile_missing_named_section_raises(tmp_path):
    cfg = _write(tmp_path, """
        [profile.default]
        workers = 3
    """)
    with pytest.raises(ProfileError, match="not found"):
        load_profile(cfg, "nope")


def test_load_profile_no_config_no_request_returns_empty(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)  # empty cwd
    assert load_profile(None, None) == {}


def test_load_profile_explicit_missing_path_raises(tmp_path):
    with pytest.raises(ProfileError, match="not found"):
        load_profile(tmp_path / "nope.toml", None)


def test_load_profile_specific_profile_without_config_raises(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)  # no sms-sender.toml here
    with pytest.raises(ProfileError):
        load_profile(None, "verify")


def test_load_profile_invalid_toml(tmp_path):
    cfg = _write(tmp_path, "not = valid = toml")
    with pytest.raises(ProfileError, match="invalid TOML"):
        load_profile(cfg, None)


def test_load_profile_picks_up_default_file_in_cwd(tmp_path, monkeypatch):
    _write(tmp_path, """
        [profile.default]
        workers = 9
    """)
    monkeypatch.chdir(tmp_path)
    assert load_profile(None, None) == {"workers": 9}


# ---------- to_default_map ----------


def test_to_default_map_expands_to_each_command():
    out = to_default_map({"workers": 5, "template": "x"}, ["send", "preview"])
    assert out == {
        "send": {"workers": 5, "template": "x"},
        "preview": {"workers": 5, "template": "x"},
    }
    # Each command gets an independent dict (no shared mutation).
    out["send"]["workers"] = 99
    assert out["preview"]["workers"] == 5


def test_to_default_map_empty_input():
    assert to_default_map({}, ["send"]) == {}


# ---------- CLI integration ----------


def test_profile_fills_required_template_for_preview(tmp_path, monkeypatch):
    _write(tmp_path, """
        [profile.default]
        template = "from-profile"
        token = "abc"
    """)
    monkeypatch.chdir(tmp_path)
    # No --template on the command line — should come from the profile.
    result = CliRunner().invoke(cli, ["preview", "--phone", "09123456789"])
    assert result.exit_code == 0, result.output
    assert "template=from-profile" in result.output
    assert "token=abc" in result.output


def test_explicit_flag_overrides_profile(tmp_path, monkeypatch):
    _write(tmp_path, """
        [profile.default]
        template = "from-profile"
    """)
    monkeypatch.chdir(tmp_path)
    result = CliRunner().invoke(
        cli, ["preview", "--phone", "09123456789", "--template", "from-cli"]
    )
    assert result.exit_code == 0, result.output
    assert "template=from-cli" in result.output
    assert "from-profile" not in result.output


def test_named_profile_via_flag(tmp_path, monkeypatch):
    _write(tmp_path, """
        [profile.default]
        template = "default-tpl"

        [profile.welcome]
        template = "welcome-tpl"
        token = "salam"
    """)
    monkeypatch.chdir(tmp_path)
    result = CliRunner().invoke(
        cli, ["--profile", "welcome", "preview", "--phone", "09123456789"]
    )
    assert result.exit_code == 0, result.output
    assert "template=welcome-tpl" in result.output
    assert "token=salam" in result.output


def test_unknown_profile_errors(tmp_path, monkeypatch):
    _write(tmp_path, """
        [profile.default]
        template = "x"
    """)
    monkeypatch.chdir(tmp_path)
    result = CliRunner().invoke(cli, ["--profile", "missing", "preview", "--phone", "0912"])
    assert result.exit_code != 0
    assert "not found" in result.output
