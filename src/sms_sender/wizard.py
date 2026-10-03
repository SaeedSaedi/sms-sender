"""Interactive wizard front-end.

Guides the user through picking an action and filling in flags, then calls
into the same code paths as the flag-driven CLI. The wizard never touches
the state DB for writes — it only peeks for the "X already sent" preview.

Resolution for `send`:
  built-in defaults  ←  picked profile (if any)  ←  wizard answers  →  _do_send

This file is the only place that knows about questionary; the rest of the
codebase stays prompt-free so non-interactive runs don't pay for it.
"""
from __future__ import annotations

import glob
import os
import sys
from pathlib import Path
from typing import Any, Callable

import click

from . import input_loader
from .config import ENV_API_KEY
from .profile import DEFAULT_CONFIG_NAME, list_profiles, load_profile
from .rate import parse_rate
from .state import (
    FAILED_PERMANENT,
    FAILED_RETRIABLE,
    PENDING,
    SENT,
    StateStore,
    campaign_db_path,
)


def _profile_db_path(profile_values: dict[str, Any]) -> str:
    """Same rule as the CLI: `state` wins, then `campaign` → data/db/<it>.db."""
    if profile_values.get("state"):
        return str(profile_values["state"])
    if profile_values.get("campaign"):
        return str(campaign_db_path(profile_values["campaign"]))
    return "./sms_state.db"


# ---------- top-level entry ----------

ACTIONS = [
    ("send", "Send SMS to a list of numbers"),
    ("preview", "Preview the request body for one number (no API call by default)"),
    ("dry-run", "Parse + normalize an input file. Show counts. No API calls."),
    ("status", "Show row counts by status from the state DB"),
    ("retry-failed", "Reset failed rows to pending and re-send"),
    ("export-failed", "Dump permanent failures to CSV"),
    ("reset", "Promote rows in some status back to pending"),
    ("purge", "Delete the state DB (no undo)"),
]


def run_wizard() -> None:
    """Top-level wizard entry. Called by `cli.wizard` and bare `sms-sender`."""
    _require_tty()
    q = _q()
    click.echo("sms-sender wizard — Ctrl-C to exit at any time.\n")
    choice = q.select(
        "What do you want to do?",
        choices=[q.Choice(title=f"{n}  —  {desc}", value=n) for n, desc in ACTIONS],
    ).ask()
    if choice is None:
        return
    dispatch: dict[str, Callable[[], None]] = {
        "send": _wizard_send,
        "preview": _wizard_preview,
        "dry-run": _wizard_dry_run,
        "status": _wizard_status,
        "retry-failed": _wizard_retry_failed,
        "export-failed": _wizard_export_failed,
        "reset": _wizard_reset,
        "purge": _wizard_purge,
    }
    dispatch[choice]()


# ---------- send (the big one) ----------

def _wizard_send() -> None:
    q = _q()
    if not os.environ.get(ENV_API_KEY, "").strip() and not Path(".env").exists():
        raise click.UsageError(
            f"{ENV_API_KEY} is not set. Export it or put it in `.env`, then re-run."
        )

    # 1. Pick (or skip) an existing profile.
    profile_values = _pick_profile_values(q)

    # 2. Pick the input file.
    input_path = _pick_input_file(q, default=profile_values.get("input"))

    # 3. Load + normalize.
    loaded = input_loader.load(input_path)
    click.echo(
        f"\nLoaded: valid={len(loaded.valid)} "
        f"invalid={len(loaded.invalid)} "
        f"duplicates_collapsed={loaded.duplicates_collapsed}"
    )
    if not loaded.valid:
        click.echo("No valid numbers in the file. Aborting.")
        return
    if loaded.invalid:
        if q.confirm(f"Show first 5 invalid rows?", default=False).ask():
            for inv in loaded.invalid[:5]:
                click.echo(f"  line {inv.line_no}: {inv.raw!r} — {inv.reason}")

    # 4. Read-only DB peek so the user sees "X new vs Y already sent".
    db_path = _profile_db_path(profile_values)
    new_count, already_sent, prior_failed = _peek_state(db_path, loaded.valid)
    click.echo(
        f"State DB ({db_path}): {new_count} new, "
        f"{already_sent} already sent, "
        f"{prior_failed} previously failed (will be retried if claimable).\n"
    )
    if new_count == 0 and prior_failed == 0:
        click.echo("Nothing to send — every number is already marked sent.")
        if not q.confirm("Continue anyway?", default=False).ask():
            return

    # 5. Template + tokens. Profile values pre-fill defaults.
    template = q.text(
        "Template name?",
        default=str(profile_values.get("template") or ""),
        validate=lambda s: bool(s.strip()) or "template is required",
    ).ask()
    if not template:
        return
    tokens = _prompt_tokens(q, profile_values)

    # 6. Advanced (collapsed by default).
    advanced = _prompt_advanced(q, profile_values) if q.confirm(
        "Configure advanced options (workers, rate, smoke-test, notify)?",
        default=False,
    ).ask() else _advanced_defaults(profile_values)

    # 7. Confirm.
    click.echo("\n" + "─" * 60)
    click.echo("About to send with these settings:")
    click.echo(f"  input             {input_path}")
    click.echo(f"  recipients        {new_count + prior_failed} to send "
               f"({already_sent} skipped as already sent)")
    click.echo(f"  template          {template}")
    for tk in ("token", "token2", "token3", "token10", "token20"):
        if tokens.get(tk):
            click.echo(f"  {tk:<17} {tokens[tk]}")
    click.echo(f"  workers           {advanced['workers']}")
    click.echo(f"  rate              {advanced['rate'] or '(unlimited)'}")
    click.echo(f"  smoke-test        {advanced['smoke_test']}")
    click.echo(f"  preflight         {not advanced['no_preflight']}")
    click.echo(f"  notify            {advanced['notify_target'] or '(off)'}")
    click.echo(f"  state DB          {advanced['db_path']}")
    click.echo("─" * 60 + "\n")
    if not q.confirm("Proceed?", default=False).ask():
        click.echo("Cancelled.")
        return

    # 8. Optional: save as profile BEFORE running, so a halt mid-run still leaves the profile.
    if q.confirm(
        "Save these answers as a reusable profile?", default=False
    ).ask():
        name = q.text(
            "Profile name?",
            validate=lambda s: bool(s.strip()) or "name is required",
        ).ask()
        if name:
            _save_profile(name.strip(), input_path, template, tokens, advanced)

    # 9. Hand off to the existing send code path.
    options = {
        "input_path": input_path,
        "template": template,
        "token": tokens.get("token"),
        "token2": tokens.get("token2"),
        "token3": tokens.get("token3"),
        "token10": tokens.get("token10"),
        "token20": tokens.get("token20"),
        "workers": advanced["workers"],
        "max_attempts": advanced["max_attempts"],
        "timeout": advanced["timeout"],
        "backoff_max": advanced["backoff_max"],
        "db_path": advanced["db_path"],
        "log_file": advanced["log_file"],
        "smoke_test": advanced["smoke_test"],
        "no_preflight": advanced["no_preflight"],
        "rate": advanced["rate"],
        "notify_target": advanced["notify_target"],
        "approval_test": False,
        "test_number": None,
        "verbose": False,
        "quiet": False,
    }
    from .cli import _do_send  # local import: avoid circular at module load
    _do_send(**options)


# ---------- send sub-prompts ----------

def _pick_profile_values(q: Any) -> dict[str, Any]:
    """Offer to load an existing profile. Returns the merged values, or {}."""
    names = list_profiles()
    if not names:
        return {}
    NEW = "__new__"
    choices = [
        q.Choice(title=f"Use profile '{n}'", value=n) for n in names
    ] + [q.Choice(title="Enter values fresh (don't load any profile)", value=NEW)]
    pick = q.select("Use an existing profile from sms-sender.toml?", choices=choices).ask()
    if pick is None or pick == NEW:
        return {}
    try:
        return load_profile(None, pick)
    except Exception as e:  # ProfileError or anything else from TOML
        click.echo(f"Could not load profile {pick!r}: {e}")
        return {}


def _pick_input_file(q: Any, default: str | None = None) -> str:
    """File picker. If .txt/.csv files exist in cwd, offer them as a list;
    otherwise fall back to a free-text path prompt. Validates existence.

    `questionary.path()` (autocomplete UI) is intentionally avoided — it
    renders blank in some terminals (notably Apple Terminal in some setups).
    """
    suggestions = sorted(set(glob.glob("*.txt") + glob.glob("*.csv")))
    if suggestions:
        OTHER = "__other__"
        choices = [q.Choice(title=s, value=s) for s in suggestions]
        choices.append(q.Choice(title="Other path…", value=OTHER))
        pick = q.select(
            "Pick an input file (.txt or .csv):", choices=choices,
        ).ask()
        if pick is None:
            raise click.Abort()
        if pick != OTHER:
            return str(Path(pick).expanduser())

    while True:
        path = q.text(
            "Path to input file (.txt or .csv):",
            default=default or "",
        ).ask()
        if path is None:
            raise click.Abort()
        p = Path(path).expanduser()
        if not p.exists() or not p.is_file():
            click.echo(f"  {p} does not exist or is not a file. Try again.")
            continue
        return str(p)


def _prompt_tokens(q: Any, profile_values: dict[str, Any]) -> dict[str, str]:
    """Pick which tokens to fill, then prompt for each.

    A profile pre-checks any token it already defines; the user can still
    deselect or override a value.
    """
    all_tokens = ["token", "token2", "token3", "token10", "token20"]
    pre_checked = [t for t in all_tokens if profile_values.get(t)]
    chosen = q.checkbox(
        "Which tokens does this template need?",
        choices=[
            q.Choice(title=t, value=t, checked=(t in pre_checked))
            for t in all_tokens
        ],
    ).ask()
    if chosen is None:
        chosen = []
    out: dict[str, str] = {}
    for name in chosen:
        out[name] = _prompt_one_token(q, name, str(profile_values.get(name) or ""))
    return out


def _prompt_one_token(q: Any, name: str, default: str) -> str:
    """token/2/3 reject spaces; token10 ≤ 5 spaces; token20 ≤ 8 spaces."""
    max_spaces = {"token": 0, "token2": 0, "token3": 0, "token10": 5, "token20": 8}[name]

    def validate(s: str) -> bool | str:
        if not s:
            return f"{name} cannot be empty"
        spaces = s.count(" ")
        if spaces > max_spaces:
            return f"{name} allows at most {max_spaces} space(s); got {spaces}"
        return True

    return q.text(f"Value for {name}?", default=default, validate=validate).ask()


def _advanced_defaults(profile_values: dict[str, Any]) -> dict[str, Any]:
    return {
        "workers": int(profile_values.get("workers", 5)),
        "max_attempts": int(profile_values.get("max_attempts", 5)),
        "timeout": float(profile_values.get("timeout", 15.0)),
        "backoff_max": float(profile_values.get("backoff_max", 30.0)),
        "db_path": _profile_db_path(profile_values),
        "log_file": str(profile_values.get("log_file", "./logs/sms-sender.log")),
        "smoke_test": bool(profile_values.get("smoke_test", False)),
        "no_preflight": bool(profile_values.get("no_preflight", False)),
        "rate": profile_values.get("rate"),
        "notify_target": profile_values.get("notify"),
    }


def _prompt_advanced(q: Any, profile_values: dict[str, Any]) -> dict[str, Any]:
    base = _advanced_defaults(profile_values)
    base["workers"] = int(q.text(
        "Workers (parallel sends)?", default=str(base["workers"]),
        validate=_validate_int(min_v=1),
    ).ask() or base["workers"])

    def rate_validator(s: str) -> bool | str:
        if not s.strip():
            return True
        try:
            parse_rate(s)
        except ValueError as e:
            return str(e)
        return True

    rate_in = q.text(
        "Rate cap (e.g. 10/s, 60/m, 3600/h, blank for unlimited)?",
        default=str(base["rate"] or ""),
        validate=rate_validator,
    ).ask()
    base["rate"] = rate_in.strip() or None

    base["smoke_test"] = q.confirm(
        "Send the first number synchronously as a smoke test before fanning out?",
        default=base["smoke_test"],
    ).ask()
    base["no_preflight"] = not q.confirm(
        "Run preflight account check before sending?",
        default=not base["no_preflight"],
    ).ask()
    notify_in = q.text(
        "Notify target on completion (slack:<url>, telegram:<bot>:<chat>, "
        "http(s)://…) — blank for off:",
        default=str(base["notify_target"] or ""),
    ).ask()
    base["notify_target"] = notify_in.strip() or None
    return base


# ---------- DB peek ----------

def _peek_state(db_path: str, valid_rows: list[Any]) -> tuple[int, int, int]:
    """Return (new, already_sent, prior_failed) without writing.

    `prior_failed` counts rows that exist with status pending / failed_retriable
    / failed_permanent / in_flight — i.e. anything that's not `sent`. From the
    user's POV: "already in the DB but not yet sent". `new` is the rest.
    """
    p = Path(db_path)
    if not p.exists():
        return (len(valid_rows), 0, 0)
    store = StateStore(db_path)
    statuses = store.status_for_phones(r.phone for r in valid_rows)
    already_sent = sum(1 for s in statuses.values() if s == SENT)
    prior_failed = sum(
        1 for s in statuses.values()
        if s in (FAILED_PERMANENT, FAILED_RETRIABLE, PENDING)
    )
    new = len(valid_rows) - len(statuses)
    return (new, already_sent, prior_failed)


# ---------- profile save ----------

def _save_profile(
    name: str,
    input_path: str,
    template: str,
    tokens: dict[str, str],
    advanced: dict[str, Any],
) -> None:
    """Append [profile.<name>] to ./sms-sender.toml. Refuses to clobber.

    Never persists secrets: API key isn't passed in here at all, and the
    notify target is *kept* (it can be a webhook with a token in the URL —
    user opted in by typing it; document this in README).
    """
    cfg = Path(DEFAULT_CONFIG_NAME)
    section = f"[profile.{name}]"
    if cfg.exists():
        existing = cfg.read_text(encoding="utf-8")
        if section in existing:
            click.echo(f"Profile {name!r} already exists in {cfg}. Skipping save.")
            return

    lines = ["", section]
    lines.append(_toml_kv("input", input_path))
    lines.append(_toml_kv("template", template))
    for tk in ("token", "token2", "token3", "token10", "token20"):
        if tokens.get(tk):
            lines.append(_toml_kv(tk, tokens[tk]))
    lines.append(_toml_kv("workers", advanced["workers"]))
    lines.append(_toml_kv("max_attempts", advanced["max_attempts"]))
    lines.append(_toml_kv("timeout", advanced["timeout"]))
    lines.append(_toml_kv("backoff_max", advanced["backoff_max"]))
    lines.append(_toml_kv("state", advanced["db_path"]))
    lines.append(_toml_kv("log_file", advanced["log_file"]))
    if advanced["smoke_test"]:
        lines.append(_toml_kv("smoke_test", True))
    if advanced["no_preflight"]:
        lines.append(_toml_kv("no_preflight", True))
    if advanced["rate"]:
        lines.append(_toml_kv("rate", advanced["rate"]))
    if advanced["notify_target"]:
        lines.append(_toml_kv("notify", advanced["notify_target"]))

    with cfg.open("a", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    click.echo(f"Saved profile {name!r} to {cfg}.")


def _toml_kv(key: str, value: Any) -> str:
    if isinstance(value, bool):
        return f"{key} = {'true' if value else 'false'}"
    if isinstance(value, (int, float)):
        return f"{key} = {value}"
    return f'{key} = "{str(value).replace(chr(92), chr(92)*2).replace(chr(34), chr(92)+chr(34))}"'


# ---------- simpler actions ----------

def _wizard_status() -> None:
    q = _q()
    db_path = q.text("Path to state DB?", default="./sms_state.db").ask()
    if db_path is None:
        return
    if not Path(db_path).exists():
        click.echo(f"No state DB at {db_path}.")
        return
    counts = StateStore(db_path).counts()
    if not counts:
        click.echo("(empty)")
        return
    width = max(len(k) for k in counts)
    for k in sorted(counts):
        click.echo(f"{k.ljust(width)}  {counts[k]}")


def _wizard_dry_run() -> None:
    q = _q()
    path = _pick_input_file(q)
    result = input_loader.load(path)
    click.echo(
        f"valid={len(result.valid)} invalid={len(result.invalid)} "
        f"duplicates_collapsed={result.duplicates_collapsed}"
    )
    for r in result.valid[:10]:
        click.echo(f"  {r.phone}  (raw={r.raw!r})")
    if len(result.valid) > 10:
        click.echo(f"  ... and {len(result.valid) - 10} more")
    for inv in result.invalid:
        click.echo(f"  INVALID line {inv.line_no}: {inv.raw!r} — {inv.reason}")


def _wizard_reset() -> None:
    q = _q()
    db_path = q.text("Path to state DB?", default="./sms_state.db").ask()
    if db_path is None:
        return
    if not Path(db_path).exists():
        click.echo(f"No state DB at {db_path}.")
        return
    from_status = q.select(
        "Promote which status back to pending?",
        choices=["failed_permanent", "failed_retriable", "sent"],
    ).ask()
    if from_status is None:
        return
    if not q.confirm(f"Reset all rows in '{from_status}' to pending?", default=False).ask():
        return
    n = StateStore(db_path).reset_status(from_status)
    click.echo(f"Reset {n} rows from {from_status} → pending")


def _wizard_purge() -> None:
    q = _q()
    db_path = q.text("Path to state DB?", default="./sms_state.db").ask()
    if db_path is None:
        return
    db = Path(db_path)
    sidecars = [db.with_name(db.name + s) for s in ("", "-wal", "-shm", "-journal")]
    existing = [p for p in sidecars if p.exists()]
    if not existing:
        click.echo(f"No state DB at {db_path}.")
        return
    if not q.confirm(
        f"Delete {len(existing)} file(s) at {db_path}*? This wipes ALL send history.",
        default=False,
    ).ask():
        return
    for p in existing:
        p.unlink()
    click.echo(f"Deleted {len(existing)} file(s).")


def _wizard_export_failed() -> None:
    q = _q()
    db_path = q.text("Path to state DB?", default="./sms_state.db").ask()
    if db_path is None:
        return
    out = q.text("Output CSV path?", default="./failed.csv").ask()
    if out is None:
        return
    import csv as _csv
    p = Path(out)
    p.parent.mkdir(parents=True, exist_ok=True)
    n = 0
    with p.open("w", newline="", encoding="utf-8") as f:
        w = _csv.writer(f)
        w.writerow(["phone_or_raw", "raw", "status_code", "attempts", "last_error"])
        for row in StateStore(db_path).iter_failed_permanent():
            w.writerow([row["phone"], row["raw"], row["status_code"],
                        row["attempts"], row["last_error"]])
            n += 1
    click.echo(f"Wrote {n} rows to {p}")


def _wizard_retry_failed() -> None:
    q = _q()
    db_path = q.text("Path to state DB?", default="./sms_state.db").ask()
    if db_path is None:
        return
    if not Path(db_path).exists():
        click.echo(f"No state DB at {db_path}. Run `send` first.")
        return
    include_perm = q.confirm(
        "Also reset failed_permanent rows? (Off by default — usually keep these.)",
        default=False,
    ).ask()
    store = StateStore(db_path)
    n = store.reset_status("failed_retriable")
    if include_perm:
        n += store.reset_status("failed_permanent")
    click.echo(f"Reset {n} row(s) to pending.")
    if n == 0:
        click.echo("Nothing to retry.")
        return
    _wizard_send()


def _wizard_preview() -> None:
    """Lightweight preview — single phone, single template."""
    q = _q()
    profile_values = _pick_profile_values(q)
    phone = q.text(
        "Phone number to preview?",
        validate=lambda s: bool(s.strip()) or "phone is required",
    ).ask()
    if phone is None:
        return
    template = q.text(
        "Template name?", default=str(profile_values.get("template") or ""),
        validate=lambda s: bool(s.strip()) or "template is required",
    ).ask()
    if template is None:
        return
    tokens = _prompt_tokens(q, profile_values)

    from .phone import InvalidPhoneError, normalize
    from .sender import Sender, SenderConfig
    try:
        canonical = normalize(phone)
    except InvalidPhoneError as e:
        click.echo(f"Invalid phone: {e}")
        return
    cfg = SenderConfig(
        api_key="<API_KEY>", template=template,
        token=tokens.get("token"), token2=tokens.get("token2"),
        token3=tokens.get("token3"), token10=tokens.get("token10"),
        token20=tokens.get("token20"),
    )
    params = Sender(cfg).build_params(canonical)
    click.echo("\nPOST https://api.kavenegar.com/v1/<API_KEY>/verify/lookup.json")
    for k, v in params.items():
        click.echo(f"  {k}={v}")


# ---------- helpers ----------

def _require_tty() -> None:
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        raise click.UsageError(
            "wizard mode requires an interactive terminal. "
            "Pass flags directly, e.g. `sms-sender send --input … --template …`. "
            "See `sms-sender send --help`."
        )


def _q():
    """Lazy import of questionary so non-wizard CLI paths don't pay for it."""
    try:
        import questionary
    except ImportError as e:  # pragma: no cover
        raise click.UsageError(
            "questionary is required for the wizard. Install with "
            "`pip install questionary`."
        ) from e
    return questionary


def _validate_int(min_v: int = 0) -> Callable[[str], bool | str]:
    def v(s: str) -> bool | str:
        try:
            n = int(s)
        except ValueError:
            return "must be an integer"
        if n < min_v:
            return f"must be ≥ {min_v}"
        return True
    return v
