"""Load CLI defaults from a TOML config file.

Format (sms-sender.toml in cwd by default):

    [profile.default]
    workers = 5
    rate = "10/s"
    log_file = "./logs/sms-sender.log"

    [profile.verify]
    template = "my-verify-template"
    token = "12345"

    [profile.welcome]
    template = "welcome"
    token = "salam"

Resolution: `[profile.default]` provides the base; the named profile overrides
it. The result is fed into `click.Context.default_map` so any CLI flag the
user didn't pass on the command line falls back to the profile value.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

try:
    import tomllib  # Python 3.11+
except ModuleNotFoundError:  # pragma: no cover
    import tomli as tomllib  # type: ignore[no-redef]


DEFAULT_CONFIG_NAME = "sms-sender.toml"
DEFAULT_PROFILE = "default"


class ProfileError(ValueError):
    """Raised on a malformed config file or unknown profile name."""


def find_config(explicit: str | Path | None) -> Path | None:
    """Resolve which config file to load, or None if none should be loaded.

    - If `explicit` is given, it must exist (raises if not).
    - Otherwise look for ./sms-sender.toml; return it if present.
    """
    if explicit is not None:
        p = Path(explicit)
        if not p.exists():
            raise ProfileError(f"config file not found: {p}")
        return p
    p = Path(DEFAULT_CONFIG_NAME)
    return p if p.exists() else None


def load_profile(
    config_path: str | Path | None,
    profile_name: str | None,
) -> dict[str, Any]:
    """Return the merged profile values, or {} if no config is in play.

    A non-None `profile_name` other than 'default' must exist in the file.
    Unknown keys pass through; CLI consumers ignore keys they don't recognize.
    """
    path = find_config(config_path)
    if path is None:
        if config_path is None and (profile_name is None or profile_name == DEFAULT_PROFILE):
            return {}
        # User asked for a specific profile but there's no config to satisfy it.
        raise ProfileError(
            f"no config file at ./{DEFAULT_CONFIG_NAME}; pass --config or create one"
        )

    with path.open("rb") as f:
        try:
            data = tomllib.load(f)
        except tomllib.TOMLDecodeError as e:
            raise ProfileError(f"invalid TOML in {path}: {e}") from e

    profiles = data.get("profile") or {}
    if not isinstance(profiles, dict):
        raise ProfileError(f"{path}: top-level [profile] must be a table")

    base: dict[str, Any] = dict(profiles.get(DEFAULT_PROFILE) or {})

    selected = profile_name or DEFAULT_PROFILE
    if selected != DEFAULT_PROFILE:
        if selected not in profiles:
            available = ", ".join(sorted(profiles)) or "(none)"
            raise ProfileError(
                f"profile {selected!r} not found in {path}. Available: {available}"
            )
        override = profiles[selected]
        if not isinstance(override, dict):
            raise ProfileError(f"{path}: [profile.{selected}] must be a table")
        base.update(override)

    return base


def list_profiles(config_path: str | Path | None = None) -> list[str]:
    """Return profile names defined in the config file, or [] if none.

    Used by the wizard to offer "pick an existing profile" — order is the
    order they appear in the TOML, with `default` (if present) first.
    """
    path = find_config(config_path)
    if path is None:
        return []
    with path.open("rb") as f:
        try:
            data = tomllib.load(f)
        except tomllib.TOMLDecodeError:
            return []
    profiles = data.get("profile") or {}
    if not isinstance(profiles, dict):
        return []
    names = list(profiles.keys())
    if DEFAULT_PROFILE in names:
        names.remove(DEFAULT_PROFILE)
        names.insert(0, DEFAULT_PROFILE)
    return names


# Profile keys whose name differs from the underlying Click parameter name.
# Click looks up `default_map[<command>][<param_name>]`, so a profile that
# uses the flag name (e.g. `state =`) needs to be remapped to the python
# identifier the option binds to (`db_path`). Keep this list in sync with
# any `--flag, "param_name"` declarations in cli.py.
_PROFILE_KEY_RENAMES: dict[str, str] = {
    "state": "db_path",
    "notify": "notify_target",
}


def to_default_map(values: dict[str, Any], commands: list[str]) -> dict[str, dict[str, Any]]:
    """Expand a flat profile dict into a Click `default_map` keyed by command.

    Each subcommand gets the same set of values; Click ignores keys that don't
    match an option of that command, so the same profile can serve `send`,
    `retry-failed`, `preview`, etc.

    Profile keys that differ from the underlying parameter name (e.g. `state`
    vs `db_path`) are translated via `_PROFILE_KEY_RENAMES` so the user can
    write the flag name and have it apply across every subcommand.
    """
    if not values:
        return {}
    translated = {
        _PROFILE_KEY_RENAMES.get(k, k): v for k, v in values.items()
    }
    return {cmd: dict(translated) for cmd in commands}
