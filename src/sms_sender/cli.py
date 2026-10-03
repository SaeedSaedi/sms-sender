"""Command-line interface."""
from __future__ import annotations

import csv
import logging
import os
import sys
from pathlib import Path
from typing import Any, Callable, TypeVar

import click

from . import input_loader, logging_config
from .config import load_api_key
from .input_loader import InputError, TokenColumns
from .notify import notify
from .phone import InvalidPhoneError, normalize as normalize_phone
from .profile import ProfileError, load_profile, to_default_map
from .rate import parse_rate
from .runner import format_report, make_runner
from .sender import TOKEN_MAX_SPACES, Sender, SenderConfig
from .state import StateStore

TEST_NUMBER_ENV = "SMS_SENDER_TEST_NUMBER"

logger = logging.getLogger(__name__)

F = TypeVar("F", bound=Callable[..., Any])


def _validate_token(ctx: click.Context, param: click.Parameter, value: str | None) -> str | None:
    # Kavenegar's space limits (see TOKEN_MAX_SPACES). Validating at the CLI
    # saves a wasted preflight round-trip on misuse.
    if value is None:
        return None
    name = param.name or ""
    limit = TOKEN_MAX_SPACES.get(name)
    if limit is None:
        return value
    spaces = value.count(" ")
    if spaces > limit:
        raise click.BadParameter(
            f"--{name} allows at most {limit} space(s); got {spaces}",
            ctx=ctx, param=param,
        )
    return value


# Per-recipient token flags, shared by `send`, `retry-failed`, `preview`, `dry-run`.
_token_column_option = click.option(
    "--token-column", "token_column", multiple=True, metavar="TOKEN=COLUMN",
    help="Fill TOKEN per recipient from a CSV column, e.g. 'token10=first_name'. "
         "Repeatable. The input must be a CSV with a header row whose first "
         "column is the phone.",
)
_value_map_option = click.option(
    "--value-map", "value_map", multiple=True, metavar="COLUMN:FROM=TO",
    help="Translate a --token-column value before sending, e.g. "
         "'trade_side:Buy=خرید'. Repeatable. A row whose value has no entry "
         "is recorded as invalid instead of being sent untranslated.",
)


def _parse_token_columns(
    token_column: tuple[str, ...], value_map: tuple[str, ...],
    static_tokens: dict[str, str | None],
) -> TokenColumns | None:
    """Turn `--token-column` / `--value-map` flags into a TokenColumns spec.

    Returns None when no --token-column is given (static tokens only).
    """
    if not token_column:
        if value_map:
            raise click.UsageError("--value-map only applies together with --token-column")
        return None
    columns: dict[str, str] = {}
    for spec in token_column:
        name, sep, column = (s.strip() for s in spec.partition("="))
        if not (sep and name and column):
            raise click.UsageError(
                f"--token-column expects TOKEN=COLUMN (e.g. token10=first_name); got {spec!r}"
            )
        if name not in TOKEN_MAX_SPACES:
            raise click.UsageError(
                f"--token-column: unknown token {name!r}; "
                f"expected one of {', '.join(TOKEN_MAX_SPACES)}"
            )
        if name in columns:
            raise click.UsageError(f"--token-column: {name} is given more than once")
        if static_tokens.get(name) is not None:
            raise click.UsageError(
                f"{name} is set both statically (--{name} or profile) and by "
                f"--token-column; pick one"
            )
        columns[name] = column
    value_maps: dict[str, dict[str, str]] = {}
    for spec in value_map:
        column, sep_col, pair = (s.strip() for s in spec.partition(":"))
        source, sep_eq, target = (s.strip() for s in pair.partition("="))
        if not (sep_col and sep_eq and column and source and target):
            raise click.UsageError(
                f"--value-map expects COLUMN:FROM=TO (e.g. trade_side:Buy=خرید); got {spec!r}"
            )
        if column not in columns.values():
            raise click.UsageError(
                f"--value-map: column {column!r} isn't used by any --token-column"
            )
        value_maps.setdefault(column, {})[source] = target
    return TokenColumns(columns=columns, value_maps=value_maps)


def _load_input(input_path: str, token_columns: TokenColumns | None) -> input_loader.LoadResult:
    try:
        return input_loader.load(input_path, token_columns)
    except InputError as e:
        raise click.UsageError(str(e)) from e


@click.group(invoke_without_command=True)
@click.option(
    "--config", "config_path", default=None, type=click.Path(dir_okay=False),
    help="TOML config file (defaults to ./sms-sender.toml if present).",
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
        # Derive the command list from the group itself so adding a new
        # subcommand automatically picks up profile defaults.
        ctx.default_map = to_default_map(values, list(cli.commands.keys()))
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
        click.option("--token", default=None, callback=_validate_token,
                     help="Static token value (no spaces)."),
        click.option("--token2", default=None, callback=_validate_token,
                     help="Static token2 value (no spaces)."),
        click.option("--token3", default=None, callback=_validate_token,
                     help="Static token3 value (no spaces)."),
        click.option("--token10", default=None, callback=_validate_token,
                     help="Static token10 value (up to 5 spaces)."),
        click.option("--token20", default=None, callback=_validate_token,
                     help="Static token20 value (up to 8 spaces)."),
        _token_column_option,
        _value_map_option,
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
            help="Send to the first claimable phone (DB order, not input order) "
                 "synchronously and abort if it fails. Catches bad "
                 "template/tokens before fanning out across the rest.",
        ),
        click.option(
            "--approval-test/--no-approval-test", default=False,
            help=f"Send a single test SMS to the operator's own number, then "
                 f"prompt y/N at the terminal before fanning out to the rest. "
                 f"The number comes from --test-number or the {TEST_NUMBER_ENV} "
                 f"env var. Out-of-band: the test send does not touch the state "
                 f"DB, so a number that is also in the input file still gets a "
                 f"normal send through the regular pipeline.",
        ),
        click.option(
            "--test-number", default=None,
            help=f"Phone number for --approval-test. Overrides the "
                 f"{TEST_NUMBER_ENV} env var.",
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


def _resolve_test_number(approval_test: bool, test_number: str | None) -> str | None:
    """Pick the test number from --test-number or the env var, normalize it.

    Returns None when --approval-test is off. When it's on, requires a number
    from one of the two sources and validates it as an Iranian mobile. Caller
    is responsible for loading `.env` (via `load_api_key`) before this runs.
    """
    if not approval_test:
        return None
    raw = test_number or os.environ.get(TEST_NUMBER_ENV)
    if not raw:
        raise click.UsageError(
            f"--approval-test requires --test-number or the {TEST_NUMBER_ENV} env var"
        )
    try:
        return normalize_phone(raw)
    except InvalidPhoneError as e:
        raise click.UsageError(f"invalid test number {raw!r}: {e}") from e


def _do_send(
    *, input_path: str, template: str,
    token: str | None, token2: str | None, token3: str | None,
    token10: str | None, token20: str | None,
    workers: int, max_attempts: int, timeout: float, backoff_max: float,
    db_path: str, log_file: str,
    smoke_test: bool, approval_test: bool, test_number: str | None,
    no_preflight: bool, rate: str | None,
    notify_target: str | None,
    verbose: bool, quiet: bool,
    # Defaulted so the wizard (static tokens only) needn't pass them.
    token_column: tuple[str, ...] = (), value_map: tuple[str, ...] = (),
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
    token_columns = _parse_token_columns(
        token_column, value_map,
        {"token": token, "token2": token2, "token3": token3,
         "token10": token10, "token20": token20},
    )

    # `load_api_key` calls `load_dotenv`, which makes `.env`-set values
    # (including SMS_SENDER_TEST_NUMBER) visible to `_resolve_test_number`.
    api_key = load_api_key()
    approval_test_number = _resolve_test_number(approval_test, test_number)
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
        approval_test_number=approval_test_number,
        token_columns=token_columns,
    )
    try:
        summary = runner.run()
    except InputError as e:
        raise click.UsageError(str(e)) from e

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

    Note: the reset is DB-wide — every `failed_retriable` row in the state
    DB is promoted, regardless of whether it appears in `--input`. The
    subsequent `send` then claims them all (unless they were already sent).
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
@click.option("--state", "db_path", default="./sms_state.db", show_default=True,
              help="Path to the SQLite state DB.")
@click.option(
    "--status", "from_status", default="failed_permanent", show_default=True,
    type=click.Choice(["failed_permanent", "failed_retriable", "sent"]),
    help="Which status to promote back to pending so it gets resent.",
)
@click.option("--yes", "-y", is_flag=True,
              help="Skip the confirmation prompt for risky resets (e.g. --status sent).")
def reset(db_path: str, from_status: str, yes: bool) -> None:
    """Promote rows in a given status back to pending so the next `send` retries them.

    Useful after fixing a template/token issue: rows that were marked
    `failed_permanent` won't be retried otherwise.

    `--status sent` is a footgun — it will cause the next `send` to deliver
    a second SMS to numbers that have already been sent to. We require an
    explicit confirmation (or `--yes`) for that case.
    """
    store = StateStore(db_path)
    if from_status == "sent" and not yes:
        click.confirm(
            "Resetting status=sent will cause the next `send` to deliver a "
            "SECOND SMS to every already-sent recipient. Are you sure?",
            abort=True,
        )
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
@_token_column_option
@_value_map_option
def dry_run(input_path: str, token_column: tuple[str, ...], value_map: tuple[str, ...]) -> None:
    """Parse + normalize + dedup, print what would be sent. No API calls."""
    result = _load_input(input_path, _parse_token_columns(token_column, value_map, {}))
    click.echo(f"valid={len(result.valid)} invalid={len(result.invalid)} "
               f"duplicates_collapsed={result.duplicates_collapsed}")
    if result.header is not None:
        click.echo(f"  (skipped header row {result.header!r})")
    for r in result.valid[:10]:
        tokens = "".join(f"  {k}={v}" for k, v in r.tokens.items())
        click.echo(f"  {r.phone}  (raw={r.raw!r}){tokens}")
    if len(result.valid) > 10:
        click.echo(f"  ... and {len(result.valid) - 10} more")
    for inv in result.invalid:
        click.echo(f"  INVALID line {inv.line_no}: {inv.raw!r} — {inv.reason}")


@cli.command()
@click.option("--template", required=True, help="Kavenegar template name.")
@click.option("--token", default=None, callback=_validate_token)
@click.option("--token2", default=None, callback=_validate_token)
@click.option("--token3", default=None, callback=_validate_token)
@click.option("--token10", default=None, callback=_validate_token)
@click.option("--token20", default=None, callback=_validate_token)
@_token_column_option
@_value_map_option
@click.option(
    "--phone", default=None,
    help="Single phone to preview (alternative to --input). With "
         "--token-column, picks that recipient's row out of --input.",
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
    token_column: tuple[str, ...], value_map: tuple[str, ...],
    phone: str | None, input_path: str | None, limit: int,
    check_account: bool, do_send: bool, timeout: float,
) -> None:
    """Show the exact request that would be POSTed for one or more phones.

    Useful as a 'show me what I'm about to do before I do it across 10k rows'
    check. With --send, sends to a single --phone for real (no state DB changes).
    """
    from .phone import InvalidPhoneError, normalize

    if not phone and not input_path:
        raise click.UsageError("Pass --phone or --input.")
    if do_send and not phone:
        raise click.UsageError("--send requires --phone (single number).")
    token_columns = _parse_token_columns(
        token_column, value_map,
        {"token": token, "token2": token2, "token3": token3,
         "token10": token10, "token20": token20},
    )
    if token_columns is not None and not input_path:
        raise click.UsageError("--token-column needs --input: the tokens come from its columns.")

    # phone → per-recipient tokens; stays empty without --token-column.
    row_tokens: dict[str, dict[str, str]] = {}
    if phone:
        try:
            phones = [normalize(phone)]
        except InvalidPhoneError as e:
            raise click.UsageError(f"invalid phone {phone!r}: {e}") from e
        if token_columns is not None:
            assert input_path is not None
            row_tokens = {r.phone: r.tokens for r in _load_input(input_path, token_columns).valid}
            if phones[0] not in row_tokens:
                raise click.UsageError(f"{phones[0]} has no valid row in {input_path}")
    else:
        assert input_path is not None
        loaded = _load_input(input_path, token_columns)
        row_tokens = {r.phone: r.tokens for r in loaded.valid}
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

    # Never echo the real key, even when --send / --check-account loaded it.
    base_url = "https://api.kavenegar.com/v1/<API_KEY>/verify/lookup.json"
    for p in phones:
        params = sender.build_params(p, row_tokens.get(p))
        click.echo(f"\nPOST {base_url}")
        for k, v in params.items():
            click.echo(f"  {k}={v}")

    if do_send:
        click.echo("\nSending …")
        result = sender.send(phones[0], tokens=row_tokens.get(phones[0]))
        click.echo(f"OK: message_id={result.message_id} status={result.status_code}")


@cli.command()
def wizard() -> None:
    """Interactive guided flow — recommended for new users."""
    from .wizard import run_wizard
    run_wizard()


if __name__ == "__main__":
    cli()
