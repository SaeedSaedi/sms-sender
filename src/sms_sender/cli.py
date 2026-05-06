"""Command-line interface."""
from __future__ import annotations

import csv
import logging
import sys
from pathlib import Path
from typing import Any, Callable, TypeVar

import click

from . import logging_config
from .config import load_api_key
from .notify import notify
from .profile import ProfileError, load_profile, to_default_map
from .rate import parse_rate
from .runner import format_report, make_runner
from .sender import Sender, SenderConfig
from .state import StateStore

logger = logging.getLogger(__name__)

F = TypeVar("F", bound=Callable[..., Any])


_PROFILE_COMMANDS = ("send", "retry-failed", "preview", "status", "export-failed", "reset", "purge", "dry-run")


@click.group(invoke_without_command=True)
@click.option(
    "--config", "config_path", default=None, type=click.Path(dir_okay=False),
    help=f"TOML config file (defaults to ./sms-sender.toml if present).",
)
@click.option(
    "--profile", "profile_name", default=None,
    help="Profile section to load from the config file (default: 'default').",
)
@click.pass_context
def cli(ctx: click.Context, config_path: str | None, profile_name: str | None) -> None:
    """Reliable bulk SMS sender for Kavenegar verify/lookup."""
    try:
        values = load_profile(config_path, profile_name)
    except ProfileError as e:
        raise click.UsageError(str(e)) from e
    if values:
        ctx.default_map = to_default_map(values, list(_PROFILE_COMMANDS))
    # Bare `sms-sender` in a TTY launches the wizard; otherwise show help.
    if ctx.invoked_subcommand is None:
        if sys.stdin.isatty() and sys.stdout.isatty():
            from .wizard import run_wizard
            run_wizard()
        else:
            click.echo(ctx.get_help())


def _send_options(f: F) -> F:
    """Flags shared by `send` and `retry-failed` so both stay in sync."""
    decorators = [
        click.option(
            "--input", "input_path", required=True,
            type=click.Path(exists=True, dir_okay=False),
            help="Path to a .txt (one number per line) or .csv (first cell per row).",
        ),
        click.option("--template", required=True, help="Kavenegar template name."),
        click.option("--token", default=None, help="Static token value (no spaces)."),
        click.option("--token2", default=None, help="Static token2 value (no spaces)."),
        click.option("--token3", default=None, help="Static token3 value (no spaces)."),
        click.option("--token10", default=None, help="Static token10 value (up to 5 spaces)."),
        click.option("--token20", default=None, help="Static token20 value (up to 8 spaces)."),
        click.option("--workers", default=5, show_default=True, type=int),
        click.option("--max-attempts", default=5, show_default=True, type=int),
        click.option("--timeout", default=15.0, show_default=True, type=float),
        click.option("--backoff-max", default=30.0, show_default=True, type=float),
        click.option(
            "--state", "db_path", default="./sms_state.db", show_default=True,
            type=click.Path(dir_okay=False),
            help="SQLite file used for resume + dedup. Re-runs reuse the same DB.",
        ),
        click.option(
            "--log-file", default="./logs/sms-sender.log", show_default=True,
            type=click.Path(dir_okay=False),
        ),
        click.option(
            "--smoke-test", is_flag=True,
            help="Send to the first phone synchronously and abort if it fails. "
                 "Catches bad template/tokens before fanning out.",
        ),
        click.option(
            "--no-preflight", is_flag=True,
            help="Skip the account-info check at the start of the run.",
        ),
        click.option(
            "--rate", "rate", default=None,
            help="Cap throughput, e.g. '10/s', '60/m', '3600/h'. Off by default. "
                 "Works on top of --workers: workers control parallelism, --rate "
                 "caps total requests per second.",
        ),
        click.option(
            "--notify", "notify_target", default=None,
            help="Post the run summary on completion. Forms: "
                 "'slack:<webhook_url>', 'telegram:<bot_token>:<chat_id>', or a "
                 "plain http(s):// URL (generic JSON webhook). Best-effort — a "
                 "failed notification never aborts the run.",
        ),
        click.option("--verbose", is_flag=True, help="DEBUG-level console output."),
        click.option("--quiet", is_flag=True, help="Only WARNING+ on console."),
    ]
    for dec in reversed(decorators):
        f = dec(f)
    return f


def _do_send(
    *, input_path: str, template: str,
    token: str | None, token2: str | None, token3: str | None,
    token10: str | None, token20: str | None,
    workers: int, max_attempts: int, timeout: float, backoff_max: float,
    db_path: str, log_file: str,
    smoke_test: bool, no_preflight: bool, rate: str | None,
    notify_target: str | None,
    verbose: bool, quiet: bool,
) -> None:
    if verbose and quiet:
        raise click.UsageError("--verbose and --quiet are mutually exclusive")
    console_level = (
        logging.DEBUG if verbose else (logging.WARNING if quiet else logging.INFO)
    )
    logging_config.setup(log_file=log_file, console_level=console_level)

    try:
        rate_per_sec = parse_rate(rate)
    except ValueError as e:
        raise click.UsageError(str(e)) from e

    api_key = load_api_key()
    sender_cfg = SenderConfig(
        api_key=api_key,
        template=template,
        token=token, token2=token2, token3=token3,
        token10=token10, token20=token20,
        timeout=timeout,
        max_attempts=max_attempts,
        backoff_max=backoff_max,
    )
    runner = make_runner(
        input_path=input_path, db_path=db_path, sender_cfg=sender_cfg, workers=workers,
        preflight=not no_preflight, smoke_test=smoke_test,
        rate_per_sec=rate_per_sec,
    )
    summary = runner.run()

    click.echo("\n" + format_report(summary))
    notify(notify_target, summary)
    if summary.halted:
        sys.exit(2)
    if summary.failed_permanent or summary.failed_retriable:
        sys.exit(1)


@cli.command()
@_send_options
def send(**kwargs: Any) -> None:
    """Send SMS to every number in INPUT, resuming from prior state."""
    _do_send(**kwargs)


@cli.command("retry-failed")
@click.option(
    "--include-permanent", is_flag=True,
    help="Also reset failed_permanent rows to pending. Off by default — those "
         "are usually permanent (bad receptor, bad template). Use after fixing "
         "a template/token issue and you want to re-attempt them.",
)
@_send_options
def retry_failed(include_permanent: bool, **kwargs: Any) -> None:
    """Reset failed rows to pending, then run `send`.

    Sugar for: `sms-sender reset --status failed_retriable && sms-sender send …`
    With `--include-permanent`, also resets failed_permanent.
    """
    store = StateStore(kwargs["db_path"])
    n = store.reset_status("failed_retriable")
    if include_permanent:
        n += store.reset_status("failed_permanent")
    click.echo(f"Reset {n} row(s) to pending.")
    if n == 0:
        click.echo("Nothing to retry.")
        return
    _do_send(**kwargs)


@cli.command()
@click.option("--state", "db_path", default="./sms_state.db", show_default=True)
def status(db_path: str) -> None:
    """Print row counts by status from the state DB."""
    store = StateStore(db_path)
    counts = store.counts()
    if not counts:
        click.echo("(empty)")
        return
    width = max(len(k) for k in counts)
    for k in sorted(counts):
        click.echo(f"{k.ljust(width)}  {counts[k]}")


@cli.command("export-failed")
@click.option("--state", "db_path", default="./sms_state.db", show_default=True)
@click.option("--out", default="./failed.csv", show_default=True, type=click.Path(dir_okay=False))
def export_failed(db_path: str, out: str) -> None:
    """Dump permanent failures to a CSV the user can fix and re-feed."""
    store = StateStore(db_path)
    p = Path(out)
    p.parent.mkdir(parents=True, exist_ok=True)
    n = 0
    with p.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["phone_or_raw", "raw", "status_code", "attempts", "last_error"])
        for row in store.iter_failed_permanent():
            w.writerow([row["phone"], row["raw"], row["status_code"], row["attempts"], row["last_error"]])
            n += 1
    click.echo(f"Wrote {n} rows to {p}")


@cli.command()
@click.option("--state", "db_path", default="./sms_state.db", show_default=True)
@click.option(
    "--status", "from_status", default="failed_permanent", show_default=True,
    type=click.Choice(["failed_permanent", "failed_retriable", "sent"]),
    help="Which status to promote back to pending so it gets resent.",
)
def reset(db_path: str, from_status: str) -> None:
    """Promote rows in a given status back to pending so the next `send` retries them.

    Useful after fixing a template/token issue: rows that were marked
    `failed_permanent` won't be retried otherwise.
    """
    store = StateStore(db_path)
    n = store.reset_status(from_status)
    click.echo(f"Reset {n} rows from {from_status} → pending")


@cli.command()
@click.option("--state", "db_path", default="./sms_state.db", show_default=True)
@click.option("--yes", "-y", is_flag=True, help="Skip the confirmation prompt.")
def purge(db_path: str, yes: bool) -> None:
    """Delete the state DB and start fresh. ALL send history is lost.

    The next `send` will treat every input number as new. Use only when you
    actually want to re-send to numbers that were already sent — there is no
    undo.
    """
    db = Path(db_path)
    sidecars = [db.with_name(db.name + s) for s in ("", "-wal", "-shm", "-journal")]
    existing = [p for p in sidecars if p.exists()]
    if not existing:
        click.echo(f"No state DB at {db_path}.")
        return
    if not yes:
        click.confirm(
            f"Delete {len(existing)} file(s) at {db_path}*? "
            "This wipes all send history (sent, failed, attempts).",
            abort=True,
        )
    for p in existing:
        p.unlink()
    click.echo(f"Deleted {len(existing)} file(s).")


@cli.command("dry-run")
@click.option("--input", "input_path", required=True, type=click.Path(exists=True, dir_okay=False))
def dry_run(input_path: str) -> None:
    """Parse + normalize + dedup, print what would be sent. No API calls."""
    from . import input_loader

    result = input_loader.load(input_path)
    click.echo(f"valid={len(result.valid)} invalid={len(result.invalid)} "
               f"duplicates_collapsed={result.duplicates_collapsed}")
    for r in result.valid[:10]:
        click.echo(f"  {r.phone}  (raw={r.raw!r})")
    if len(result.valid) > 10:
        click.echo(f"  ... and {len(result.valid) - 10} more")
    for inv in result.invalid:
        click.echo(f"  INVALID line {inv.line_no}: {inv.raw!r} — {inv.reason}")


@cli.command()
@click.option("--template", required=True, help="Kavenegar template name.")
@click.option("--token", default=None)
@click.option("--token2", default=None)
@click.option("--token3", default=None)
@click.option("--token10", default=None)
@click.option("--token20", default=None)
@click.option(
    "--phone", default=None,
    help="Single phone to preview (alternative to --input).",
)
@click.option(
    "--input", "input_path", default=None,
    type=click.Path(exists=True, dir_okay=False),
    help="Preview the first --limit phones from this file.",
)
@click.option("--limit", default=5, show_default=True, type=int)
@click.option(
    "--check-account", is_flag=True,
    help="Also call Kavenegar account/info to confirm the API key works.",
)
@click.option(
    "--send", "do_send", is_flag=True,
    help="Actually send to --phone. Requires --phone (single number) and "
         "does NOT touch the state DB.",
)
@click.option("--timeout", default=15.0, show_default=True, type=float)
def preview(
    template: str, token: str | None, token2: str | None, token3: str | None,
    token10: str | None, token20: str | None,
    phone: str | None, input_path: str | None, limit: int,
    check_account: bool, do_send: bool, timeout: float,
) -> None:
    """Show the exact request that would be POSTed for one or more phones.

    Useful as a 'show me what I'm about to do before I do it across 10k rows'
    check. With --send, sends to a single --phone for real (no state DB changes).
    """
    from . import input_loader
    from .phone import InvalidPhoneError, normalize

    if not phone and not input_path:
        raise click.UsageError("Pass --phone or --input.")
    if do_send and not phone:
        raise click.UsageError("--send requires --phone (single number).")

    if phone:
        try:
            phones = [normalize(phone)]
        except InvalidPhoneError as e:
            raise click.UsageError(f"invalid phone {phone!r}: {e}") from e
    else:
        assert input_path is not None
        loaded = input_loader.load(input_path)
        phones = [r.phone for r in loaded.valid[:limit]]
        click.echo(
            f"Loaded {len(loaded.valid)} valid, {len(loaded.invalid)} invalid; "
            f"previewing first {len(phones)}."
        )

    api_key = load_api_key() if (do_send or check_account) else "<API_KEY>"
    cfg = SenderConfig(
        api_key=api_key, template=template,
        token=token, token2=token2, token3=token3,
        token10=token10, token20=token20,
        timeout=timeout,
    )
    sender = Sender(cfg)

    if check_account:
        info = sender.account_info()
        click.echo(
            f"Account: credit={info.remaining_credit} "
            f"expires={info.expire_date or '?'} type={info.type or '?'}"
        )

    base_url = f"https://api.kavenegar.com/v1/{api_key}/verify/lookup.json"
    for p in phones:
        params = sender.build_params(p)
        click.echo(f"\nPOST {base_url}")
        for k, v in params.items():
            click.echo(f"  {k}={v}")

    if do_send:
        click.echo("\nSending …")
        result = sender.send(phones[0])
        click.echo(f"OK: message_id={result.message_id} status={result.status_code}")


@cli.command()
def wizard() -> None:
    """Interactive guided flow — recommended for new users."""
    from .wizard import run_wizard
    run_wizard()


if __name__ == "__main__":
    cli()
