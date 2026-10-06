"""Command-line interface."""
from __future__ import annotations

import csv
import json
import logging
import os
import sys
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Iterator, TypeVar

import click
from click.core import ParameterSource
from dotenv import load_dotenv

from . import input_loader, logging_config
from .allowlist import ENV_ALLOWED_NUMBERS
from .config import load_api_key
from .input_loader import SLUG_RE, InputError, TokenColumns
from .links import (
    DEFAULT_EXPIRY_DAYS,
    DEFAULT_RATE as DEFAULT_LINK_RATE,
    ENV_LINK_DOMAINS,
    STRATEGIES as LINK_STRATEGIES,
    LinkSettings,
    allowed_domains,
    destination_problem,
    format_problem as link_format_problem,
    placeholder_token,
    plan_link,
)
from .locking import RunLock, RunLockError
from .notify import notify
from .phone import InvalidPhoneError, normalize as normalize_phone
from .profile import ProfileError, load_profile, to_default_map
from .rate import parse_rate
from .reconcile import DEFAULT_MIN_AGE_SEC, REQUEUE_NOT_FOUND, reconcile_unknown
from .runner import format_report, make_runner
from .sendcheck import check_sends
from .delivery import describe as describe_delivery
from .delivery import sync_delivery
from .sender import (
    TOKEN_MAX_SPACES,
    HaltError,
    SendError,
    Sender,
    SenderConfig,
    token_problem,
)
from .clicks import (
    ATTRIBUTION_HEADER,
    CLICKERS_HEADER,
    attribution_rows,
    click_report,
    clicker_rows,
    sync_clicks,
)
from .clicks import describe as describe_clicks
from .shortlink import (
    ShlinkClient,
    ShlinkError,
    ShlinkHaltError,
    load_shlink_config,
    shlink_base_url,
)
from .frequency import parse_cap
from .window import DEFAULT_WINDOW, ENV_SEND_WINDOW, parse_window
from . import sharing
from .state import (
    CAMPAIGN_DB_DIR,
    CANCELLED,
    INVALID,
    NEEDS_REVIEW,
    SUPPRESSED,
    UNKNOWN,
    CampaignMismatchError,
    StateStore,
    campaign_db_path,
)

TEST_NUMBER_ENV = "SMS_SENDER_TEST_NUMBER"
# Where the log goes when --log-file isn't given (tests point it at a temp folder).
LOG_FILE_ENV = "SMS_SENDER_LOG_FILE"

logger = logging.getLogger(__name__)

F = TypeVar("F", bound=Callable[..., Any])


def _validate_token(ctx: click.Context, param: click.Parameter, value: str | None) -> str | None:
    # Kavenegar's token rules (see `token_problem`). Validating at the CLI
    # saves a wasted preflight round-trip on misuse.
    if value is None:
        return None
    name = param.name or ""
    if name not in TOKEN_MAX_SPACES:
        return value
    problem = token_problem(name, value)
    if problem:
        raise click.BadParameter(f"--{problem}", ctx=ctx, param=param)
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
_user_id_column_option = click.option(
    "--user-id-column", "user_id_column", default=None, metavar="COLUMN",
    help="CSV column with each recipient's user ID (the input needs a header "
         "row; its first column is the phone). A blank ID is still sent and "
         "reported as missing user ID; a phone with two different IDs isn't sent.",
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


_LINK_OPTIONS = [
    click.option(
        "--link-url", "link_url", default=None, metavar="URL",
        help="Where the campaign's short links lead (https, on an allowed domain: "
             f"{ENV_LINK_DOMAINS}, default kifpool.me). Turns on the link stage: "
             "every link is made at Shlink before any SMS goes out. Needs "
             "--campaign and --link-token.",
    ),
    click.option(
        "--link-token", "link_token", default=None, type=click.Choice(list(TOKEN_MAX_SPACES)),
        help="Which template token carries the link.",
    ),
    click.option(
        "--link-format", "link_format", default="url", show_default=True,
        help="What the token holds. url: the whole https://kifpool.me/u/<code>. "
             "code: only <code>, for a template whose text already has "
             "https://kifpool.me/u/. Or a pattern with {code} for a template whose "
             "text holds part of it, e.g. 'u/{code}' after https://kifpool.me/.",
    ),
    click.option(
        "--link-strategy", "link_strategy", default="recipient", show_default=True,
        type=click.Choice(LINK_STRATEGIES),
        help="recipient: one link per person (clicks per recipient). segment: one "
             "per segment. campaign: one for everyone.",
    ),
    click.option(
        "--link-expiry-days", "link_expiry_days", default=DEFAULT_EXPIRY_DAYS,
        show_default=True, type=int, help="How long links keep working.",
    ),
    click.option("--utm-source", "utm_source", default="sms", show_default=True),
    click.option("--utm-medium", "utm_medium", default="sms", show_default=True),
    click.option("--utm-campaign", "utm_campaign", default=None,
                 help="Default: the campaign name."),
    click.option("--utm-content", "utm_content", default=None,
                 help="Default: each recipient's segment (none on a campaign-wide link)."),
    click.option(
        "--link-rate", "link_rate", default=DEFAULT_LINK_RATE, show_default=True,
        help="Cap on link creation at Shlink, e.g. '10/s'. Conservative until "
             "Shlink's real speed is measured.",
    ),
]


def _link_options(f: F) -> F:
    for dec in reversed(_LINK_OPTIONS):
        f = dec(f)
    return f


def _link_settings(
    *, link_url: str | None, link_token: str | None, link_format: str,
    link_strategy: str, link_expiry_days: int, utm_source: str, utm_medium: str,
    utm_campaign: str | None, utm_content: str | None, campaign: str | None,
    static_tokens: dict[str, str | None], token_columns: TokenColumns | None,
) -> LinkSettings | None:
    """The campaign's link settings from the flags, or None without --link-url."""
    if link_url is None:
        if link_token is not None:
            raise click.UsageError("--link-token only applies together with --link-url")
        return None
    if not campaign:
        raise click.UsageError(
            "--link-url needs --campaign: links are tagged, tracked and reported per campaign"
        )
    if link_token is None:
        raise click.UsageError("--link-url needs --link-token: which template token carries it")
    if static_tokens.get(link_token) is not None:
        raise click.UsageError(f"{link_token} carries the link; don't also set --{link_token}")
    if token_columns is not None and link_token in token_columns.columns:
        raise click.UsageError(
            f"{link_token} carries the link; don't also fill it with --token-column"
        )
    links = LinkSettings(
        destination=link_url, token=link_token, format=link_format, strategy=link_strategy,
        expiry_days=link_expiry_days, utm_source=utm_source, utm_medium=utm_medium,
        utm_campaign=utm_campaign, utm_content=utm_content,
    )
    problems = links.problems()
    if problems:
        raise click.UsageError("; ".join(problems))
    problem = destination_problem(link_url, allowed_domains(), shlink_base_url())
    if problem:
        raise click.BadParameter(problem, param_hint="--link-url")
    return links


def _load_input(
    input_path: str, token_columns: TokenColumns | None, user_id_column: str | None = None,
) -> input_loader.LoadResult:
    try:
        return input_loader.load(input_path, token_columns, user_id_column)
    except InputError as e:
        raise click.UsageError(str(e)) from e


def _load_opt_out(paths: tuple[str, ...]) -> frozenset[str] | None:
    """Phones that must never get the campaign, from one or more lists.
    Lines that aren't phone numbers are skipped."""
    if not paths:
        return None
    phones: set[str] = set()
    for path in paths:
        phones.update(r.phone for r in _load_input(path, None).valid)
    return frozenset(phones)


_campaign_option = click.option(
    "--campaign", default=None, metavar="SLUG",
    help="Campaign name, e.g. 'coin-price-7'. Gives the campaign its own state "
         "DB, data/db/<SLUG>.db, unless --state is given.",
)


def _resolve_db_path(db_path: str, campaign: str | None) -> str:
    """Which state DB a command uses: --state (flag or profile) wins, then
    `--campaign X` → data/db/X.db, then the built-in default."""
    if campaign is None:
        return db_path
    if not SLUG_RE.match(campaign):
        raise click.BadParameter(
            f"{campaign!r}: use lowercase letters, digits and dashes, e.g. coin-price-7",
            param_hint="--campaign",
        )
    source = click.get_current_context().get_parameter_source("db_path")
    if source not in (ParameterSource.DEFAULT, None):
        return db_path
    path = campaign_db_path(campaign)
    path.parent.mkdir(parents=True, exist_ok=True)
    return str(path)


@contextmanager
def _db_lock(db_path: str) -> Iterator[None]:
    """Hold the state DB's run lock, or exit 2 if another process has it."""
    lock = RunLock(db_path)
    try:
        lock.acquire()
    except RunLockError as e:
        click.echo(f"Error: {e}", err=True)
        sys.exit(2)
    try:
        yield
    finally:
        lock.release()


def _guard_folder(db_path: str, *, changes: bool = True) -> None:
    """A dashboard worker on another kernel (Docker Desktop's VM) is using
    this DB's folder: locks don't reach across, so a command that changes
    rows could claim what the worker is sending. Refuse those (exit 2), and
    warn for reads, which may see the folder a moment behind."""
    other = sharing.foreign_worker(Path(db_path).parent)
    if other is None:
        return
    where = Path(db_path).parent
    if changes:
        click.echo(
            f"Error: the dashboard's worker ({other.get('worker', '?')}) is using {where} from another "
            "system (for example inside Docker), and file locks don't reach across. Run this inside "
            "the container instead (docker compose exec worker sms-sender ...), or stop the worker first.",
            err=True,
        )
        sys.exit(2)
    click.echo(
        f"Warning: the dashboard's worker is using {where} from another system; what this shows may "
        "be a moment behind. Inside the container it isn't (docker compose exec worker sms-sender ...).",
        err=True,
    )


def _refuse_while_held(folder: Path) -> None:
    """An admin held all sending on the dashboard (its emergency stop):
    no send starts from this folder until they lift it (exit 2)."""
    hold = sharing.held(folder)
    if hold is None:
        return
    since = f" since {time.strftime('%Y-%m-%d %H:%M', time.localtime(hold['at']))}" if hold.get("at") else ""
    if hold.get("reason") == sharing.MOVED:
        click.echo(
            f"Error: this data moved to the server{since} (sms-dashboard retire), so nothing is sent from "
            f"{folder}: send from the server. A send from this copy too could reach people twice.",
            err=True,
        )
        sys.exit(2)
    click.echo(
        f"Error: all sending is held from the dashboard (by {hold.get('by') or '?'}{since}). Nothing is "
        f"sent from {folder} until an admin lifts the hold on the dashboard's status page.",
        err=True,
    )
    sys.exit(2)


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
    # `.env` first, before any subcommand reads a setting: options with an
    # env var (e.g. --send-window) and SMS_SENDER_LINK_DOMAINS see it too.
    # Never overrides a variable that's already set.
    load_dotenv(".env")
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
        _user_id_column_option,
        click.option(
            "--segment", default=None, metavar="NAME",
            help="Which segment this input is (lowercase letters, digits, dashes); "
                 "defaults to the input file's name. Recorded per recipient for "
                 "reports and links.",
        ),
        click.option("--workers", default=5, show_default=True, type=int),
        click.option("--max-attempts", default=5, show_default=True, type=int),
        click.option("--timeout", default=15.0, show_default=True, type=float),
        click.option("--backoff-max", default=30.0, show_default=True, type=float),
        click.option(
            "--state", "db_path", default="./sms_state.db", show_default=True,
            type=click.Path(dir_okay=False),
            help="SQLite file used for resume + dedup. Re-runs reuse the same DB.",
        ),
        _campaign_option,
        click.option(
            "--opt-out", "opt_out", multiple=True,
            type=click.Path(exists=True, dir_okay=False),
            help="A list of phones that must never get this campaign (same "
                 "formats as --input). Repeatable. Matching recipients become "
                 "`suppressed`; anyone already sent stays `sent`.",
        ),
        click.option(
            "--frequency-cap", "frequency_cap", default=None, metavar="N/DAYS",
            help="At most N SMS to one number in DAYS days, counted across the "
                 "campaign DBs in this DB's folder, e.g. 2/7. Recipients over it "
                 "become `capped` and are counted again at the next run. Off by default.",
        ),
        click.option(
            "--send-window", "send_window", default=DEFAULT_WINDOW, show_default=True,
            envvar=ENV_SEND_WINDOW, metavar="HH:MM-HH:MM",
            help="Only send inside this daily window, in Tehran time; 'off' to send "
                 "any time. Outside it the run won't start, and a run stops "
                 "(resumably) when the window closes.",
        ),
        click.option(
            "--allow-settings-change", is_flag=True,
            help="Let a campaign that already sent continue with a different "
                 "template or tokens (both message versions end up in one campaign).",
        ),
        click.option(
            "--log-file", default="./logs/sms-sender.log", show_default=True,
            type=click.Path(dir_okay=False), envvar=LOG_FILE_ENV,
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
    for dec in reversed(decorators + _LINK_OPTIONS):
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
    campaign: str | None = None, allow_settings_change: bool = False,
    opt_out: tuple[str, ...] = (),
    frequency_cap: str | None = None,
    # None: the env var, else 08:00-21:00 (the wizard doesn't pass it).
    send_window: str | None = None,
    user_id_column: str | None = None,
    segment: str | None = None,
    link_url: str | None = None, link_token: str | None = None,
    link_format: str = "url", link_strategy: str = "recipient",
    link_expiry_days: int = DEFAULT_EXPIRY_DAYS,
    utm_source: str = "sms", utm_medium: str = "sms",
    utm_campaign: str | None = None, utm_content: str | None = None,
    link_rate: str | None = DEFAULT_LINK_RATE,
) -> None:
    if verbose and quiet:
        raise click.UsageError("--verbose and --quiet are mutually exclusive")
    if segment is not None and not SLUG_RE.match(segment):
        raise click.BadParameter(
            f"{segment!r}: use lowercase letters, digits and dashes, e.g. vip-2",
            param_hint="--segment",
        )
    db_path = _resolve_db_path(db_path, campaign)
    _guard_folder(db_path)
    _refuse_while_held(Path(db_path).parent)
    console_level = (
        logging.DEBUG if verbose else (logging.WARNING if quiet else logging.INFO)
    )
    logging_config.setup(log_file=log_file, console_level=console_level)

    try:
        rate_per_sec = parse_rate(rate)
    except ValueError as e:
        raise click.UsageError(str(e)) from e
    static_tokens = {"token": token, "token2": token2, "token3": token3,
                     "token10": token10, "token20": token20}
    token_columns = _parse_token_columns(token_column, value_map, static_tokens)
    links = _link_settings(
        link_url=link_url, link_token=link_token, link_format=link_format,
        link_strategy=link_strategy, link_expiry_days=link_expiry_days,
        utm_source=utm_source, utm_medium=utm_medium, utm_campaign=utm_campaign,
        utm_content=utm_content, campaign=campaign, static_tokens=static_tokens,
        token_columns=token_columns,
    )
    try:
        link_rate_per_sec = parse_rate(link_rate)
    except ValueError as e:
        raise click.UsageError(f"--link-rate: {e}") from e
    if send_window is None:
        send_window = os.environ.get(ENV_SEND_WINDOW, DEFAULT_WINDOW)
    try:
        window = parse_window(send_window)
    except ValueError as e:
        raise click.UsageError(str(e)) from e
    try:
        cap = parse_cap(frequency_cap)
    except ValueError as e:
        raise click.UsageError(f"--frequency-cap: {e}") from e

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
    link_client = None
    if links is not None:
        try:
            link_client = ShlinkClient(load_shlink_config(timeout=timeout))
        except RuntimeError as e:
            raise click.UsageError(str(e)) from e
    runner = make_runner(
        input_path=input_path, db_path=db_path, sender_cfg=sender_cfg, workers=workers,
        preflight=not no_preflight, smoke_test=smoke_test,
        rate_per_sec=rate_per_sec,
        approval_test_number=approval_test_number,
        token_columns=token_columns,
        campaign=campaign,
        allow_settings_change=allow_settings_change,
        opt_out=_load_opt_out(opt_out),
        frequency_cap=cap,
        send_window=window,
        user_id_column=user_id_column,
        segment=segment,
        links=links,
        link_client=link_client,
        link_rate_per_sec=link_rate_per_sec,
    )
    try:
        summary = runner.run()
    except InputError as e:
        raise click.UsageError(str(e)) from e
    except (RunLockError, CampaignMismatchError) as e:
        click.echo(f"Error: {e}", err=True)
        sys.exit(2)

    click.echo("\n" + format_report(summary))
    if summary.unknown:
        click.echo(
            "Unknown rows are never resent automatically. In 5+ minutes, run "
            f"`sms-sender reconcile --state {db_path}` to check them with Kavenegar."
        )
    if summary.needs_review:
        click.echo(
            "needs_review rows may already have the SMS. Only if you're sure they "
            f"don't: `sms-sender reset --status needs_review --state {db_path}`."
        )
    if summary.stopped:
        click.echo("Stopped before finishing. Re-run the same command to continue.")
    notify(notify_target, summary)
    if summary.halted:
        sys.exit(2)
    if (summary.failed_permanent or summary.failed_retriable
            or summary.unknown or summary.needs_review or summary.stopped):
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
    kwargs["db_path"] = _resolve_db_path(kwargs["db_path"], kwargs.get("campaign"))
    _guard_folder(kwargs["db_path"])
    _refuse_while_held(Path(kwargs["db_path"]).parent)
    store = StateStore(kwargs["db_path"])
    with _db_lock(kwargs["db_path"]):
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
@_campaign_option
def status(db_path: str, campaign: str | None) -> None:
    """Print the campaign, its template and last run, and row counts by status."""
    db_path = _resolve_db_path(db_path, campaign)
    _guard_folder(db_path, changes=False)
    if not Path(db_path).exists():
        click.echo(f"(no state DB at {db_path})")
        return
    store = StateStore(db_path)
    name, settings, last_run = (
        store.get_meta("campaign"), store.get_meta("settings"), store.get_meta("last_run"),
    )
    if name:
        click.echo(f"campaign   {name}")
    if settings:
        click.echo(f"template   {json.loads(settings).get('template')}")
    if last_run:
        run = json.loads(last_run)
        when = time.strftime("%Y-%m-%d %H:%M", time.localtime(run["at"]))
        flags = "".join(
            f", {flag}" for flag in ("halted", "stopped") if run.get(flag)
        )
        click.echo(
            f"last run   {when}: sent {run['sent']}, "
            f"failed {run['failed_permanent'] + run['failed_retriable']}{flags}"
        )
    delivered = store.delivery_counts()
    if delivered:
        click.echo(f"delivery   {describe_delivery(delivered)}")
    if store.get_meta("user_id_column") is not None:
        with_id, missing = store.user_id_counts()
        click.echo(f"user IDs   {with_id} recipient(s); missing user ID: {missing}")
    links = store.link_counts()
    if links:
        click.echo("links      " + " · ".join(
            f"{status} {links[status]}" for status in ("ready", "pending", "failed")
            if links.get(status)
        ))
    synced = store.last_click_sync()
    if synced is not None:
        segments, campaign_clicks = click_report(store)
        total = sum(s.clicks for s in segments) + campaign_clicks
        when = time.strftime("%Y-%m-%d %H:%M", time.localtime(synced))
        personal = [s for s in segments if s.clicked is not None]
        clicked = (
            f"; clicked {sum(s.clicked or 0 for s in personal)} of "
            f"{sum(s.sent for s in personal)} recipients" if personal else ""
        )
        click.echo(f"clicks     {total} (bots excluded, synced {when}){clicked}")
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
@_campaign_option
def export_failed(db_path: str, out: str, campaign: str | None) -> None:
    """Dump permanent failures to a CSV the user can fix and re-feed."""
    db_path = _resolve_db_path(db_path, campaign)
    _guard_folder(db_path, changes=False)
    store = StateStore(db_path)
    p = Path(out)
    p.parent.mkdir(parents=True, exist_ok=True)
    n = 0
    with p.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(StateStore.FAILED_HEADER)
        for row in store.iter_failed_permanent():
            w.writerow([row["phone"], row["raw"], row["status_code"], row["attempts"], row["last_error"]])
            n += 1
    click.echo(f"Wrote {n} rows to {p}")


@cli.command()
@click.option("--state", "db_path", default="./sms_state.db", show_default=True,
              help="Path to the SQLite state DB.")
@click.option(
    "--status", "from_status", default="failed_permanent", show_default=True,
    type=click.Choice([
        "failed_permanent", "failed_retriable", "sent", UNKNOWN, NEEDS_REVIEW, SUPPRESSED,
        INVALID, CANCELLED,
    ]),
    help="Which status to promote back to pending so it gets resent.",
)
@click.option("--yes", "-y", is_flag=True,
              help="Skip the confirmation prompt for risky resets (e.g. --status sent).")
@_campaign_option
def reset(db_path: str, from_status: str, yes: bool, campaign: str | None) -> None:
    """Promote rows in a given status back to pending so the next `send` retries them.

    Useful after fixing a template/token issue: rows that were marked
    `failed_permanent` won't be retried otherwise.

    `--status sent`, `unknown` and `needs_review` are footguns — those rows
    have, or may have, the SMS already, so the next `send` can deliver a
    second one. They need an explicit confirmation (or `--yes`). For
    `unknown`, prefer `sms-sender reconcile`, which checks with Kavenegar.
    """
    db_path = _resolve_db_path(db_path, campaign)
    _guard_folder(db_path)
    store = StateStore(db_path)
    risky = {
        "sent": "Resetting status=sent will cause the next `send` to deliver a "
                "SECOND SMS to every already-sent recipient. Are you sure?",
        UNKNOWN: "These rows may already have the SMS. Resetting them sends again "
                 "without checking — `sms-sender reconcile` checks with Kavenegar "
                 "first. Are you sure?",
        NEEDS_REVIEW: "These rows may already have the SMS (the Kavenegar check found "
                      "several candidate messages). Resetting them sends again. "
                      "Are you sure?",
        SUPPRESSED: "These recipients are on the opt-out list. Resetting them sends "
                    "them the campaign. Are you sure?",
        INVALID: "These phones came with two different user IDs, so their clicks "
                 "can't be attributed. Reset only after fixing the IDs at the source; "
                 "a run with --user-id-column checks them again. Are you sure?",
    }
    if from_status in risky and not yes:
        click.confirm(risky[from_status], abort=True)
    with _db_lock(db_path):
        n = store.reset_status(from_status)
    click.echo(f"Reset {n} rows from {from_status} → pending")


@cli.command()
@click.option("--state", "db_path", default="./sms_state.db", show_default=True)
@click.option("--yes", "-y", is_flag=True, help="Skip the confirmation prompt.")
@_campaign_option
def purge(db_path: str, yes: bool, campaign: str | None) -> None:
    """Delete the state DB and start fresh. ALL send history is lost.

    The next `send` will treat every input number as new. Use only when you
    actually want to re-send to numbers that were already sent — there is no
    undo.
    """
    db_path = _resolve_db_path(db_path, campaign)
    _guard_folder(db_path)
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
    # Never delete a DB out from under a running send.
    with _db_lock(db_path):
        for p in existing:
            p.unlink()
    click.echo(f"Deleted {len(existing)} file(s).")


@cli.command()
@click.option("--state", "db_path", default="./sms_state.db", show_default=True,
              type=click.Path(dir_okay=False))
@click.option(
    "--min-age", "min_age", default=DEFAULT_MIN_AGE_SEC, show_default=True, type=float,
    help="Only check rows whose last attempt is at least this many seconds old.",
)
@click.option(
    "--requeue-not-found/--review-not-found", default=REQUEUE_NOT_FOUND, show_default=True,
    help="What 'Kavenegar has no message for this phone on the attempt's day' "
         "means: requeue (it was never sent; only for attempts under a day old, "
         "older ones always go to review) or needs_review.",
)
@click.option("--timeout", default=15.0, show_default=True, type=float)
@click.option(
    "--log-file", default="./logs/sms-sender.log", show_default=True,
    type=click.Path(dir_okay=False), envvar=LOG_FILE_ENV,
)
@_campaign_option
def reconcile(
    db_path: str, min_age: float, requeue_not_found: bool, timeout: float, log_file: str,
    campaign: str | None,
) -> None:
    """Ask Kavenegar what happened to `unknown` rows. Never sends anything.

    Found at Kavenegar → `sent`. Several candidate messages → `needs_review`.
    Not found on the attempt's day → `failed_retriable`, so the next `send`
    delivers it (attempts over a day old, or with --review-not-found:
    `needs_review`).
    """
    logging_config.setup(log_file=log_file, console_level=logging.WARNING)
    db_path = _resolve_db_path(db_path, campaign)
    _guard_folder(db_path)
    if not Path(db_path).exists():
        raise click.UsageError(f"No state DB at {db_path}.")
    store = StateStore(db_path)
    # Read-only lookups: the template is never used.
    sender = Sender(SenderConfig(api_key=load_api_key(), template="", timeout=timeout))
    with _db_lock(db_path):
        try:
            result = reconcile_unknown(
                store, sender, min_age_sec=min_age, requeue_not_found=requeue_not_found,
            )
        except HaltError as e:
            click.echo(
                f"Error: Kavenegar refused the lookup: [{e.status_code}] {e.message}",
                err=True,
            )
            sys.exit(2)
    click.echo(f"sent (found at Kavenegar)      {result.sent}")
    click.echo(f"not sent (safe to send again)  {result.requeued}")
    click.echo(f"needs review                   {result.needs_review}")
    if result.deferred:
        click.echo(
            f"not checked yet                {result.deferred}  (last attempt under "
            f"{min_age:.0f}s ago, or Kavenegar unreachable: run again later)"
        )
    left = store.counts()
    if left.get(UNKNOWN) or left.get(NEEDS_REVIEW):
        sys.exit(1)


@cli.command()
@click.option("--state", "db_path", default="./sms_state.db", show_default=True,
              type=click.Path(dir_okay=False))
@_campaign_option
@click.option("--timeout", default=15.0, show_default=True, type=float)
@click.option(
    "--log-file", default="./logs/sms-sender.log", show_default=True,
    type=click.Path(dir_okay=False), envvar=LOG_FILE_ENV,
)
def delivery(db_path: str, campaign: str | None, timeout: float, log_file: str) -> None:
    """Fetch delivery reports for SMS sent in the last 48 h. Never sends anything.

    Kavenegar only reports delivery for 48 hours after sending, so run this a
    few times in that window (e.g. after 10 minutes, an hour, a day). Safe to
    run while a send is in progress.
    """
    logging_config.setup(log_file=log_file, console_level=logging.WARNING)
    db_path = _resolve_db_path(db_path, campaign)
    _guard_folder(db_path)
    if not Path(db_path).exists():
        raise click.UsageError(f"No state DB at {db_path}.")
    store = StateStore(db_path)
    # Read-only lookups: the template is never used.
    sender = Sender(SenderConfig(api_key=load_api_key(), template="", timeout=timeout))
    try:
        result = sync_delivery(store, sender)
    except SendError as e:  # incl. HaltError
        click.echo(f"Error: Kavenegar didn't answer: [{e.status_code}] {e.message}", err=True)
        sys.exit(2 if isinstance(e, HaltError) else 1)
    click.echo(f"checked {result.checked} SMS, {result.updated} with a status")
    click.echo(f"delivery   {describe_delivery(store.delivery_counts()) or '(nothing sent)'}")


@cli.command("check-sends")
@click.option("--state", "db_path", default="./sms_state.db", show_default=True,
              type=click.Path(dir_okay=False))
@_campaign_option
def check_sends_command(db_path: str, campaign: str | None) -> None:
    """Check that nobody got the campaign twice. Never sends anything.

    Counts each phone's SMS from the record of every call to Kavenegar,
    approval tests apart. Exits 1 if a phone got two or more, or may have
    (an undecided call next to an accepted one); 0 otherwise. Safe to run
    while a send is in progress."""
    _, store, name = _campaign_store(db_path, campaign)
    result = check_sends(store)
    click.echo(f"campaign    {name}")
    click.echo(f"SMS         {result.sms} accepted for {result.recipients} recipient(s)")
    if result.test_sms:
        click.echo(f"test SMS    {result.test_sms} (approval tests, not counted above)")
    if result.unsettled:
        click.echo(f"unsettled   {result.unsettled} row(s) unknown or needs_review: "
                   "`sms-sender reconcile` settles them")
    if result.unrecorded:
        click.echo(f"unrecorded  {result.unrecorded} sent row(s) without a call record "
                   "(sent before sms-sender kept them): not checked")
    for p in result.twice:
        click.echo(f"TWICE       {p.phone}: messages {', '.join(map(str, p.message_ids))}")
    for p in result.maybe_twice:
        sent = ", ".join(map(str, p.message_ids)) or "none confirmed"
        click.echo(f"MAYBE       {p.phone}: messages {sent}, plus {p.undecided} undecided call(s)")
    if not result.ok:
        click.echo(f"{len(result.twice)} phone(s) got it twice, {len(result.maybe_twice)} may have.")
        sys.exit(1)
    click.echo("OK: nobody got this campaign twice.")


def _campaign_store(db_path: str, campaign: str | None, *, changes: bool = False) -> tuple[str, StateStore, str]:
    """(db path, store, campaign name) for commands that read a campaign's
    DB. Never creates one."""
    db_path = _resolve_db_path(db_path, campaign)
    _guard_folder(db_path, changes=changes)
    if not Path(db_path).exists():
        raise click.UsageError(f"No state DB at {db_path}.")
    store = StateStore(db_path)
    name = campaign or store.get_meta("campaign") or Path(db_path).stem
    return db_path, store, name


def _write_csv(path: Path, header: list[str], rows: Iterator[list]) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    n = 0
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(header)
        for row in rows:
            w.writerow(row)
            n += 1
    return n


@cli.command()
@click.option("--state", "db_path", default="./sms_state.db", show_default=True,
              type=click.Path(dir_okay=False))
@_campaign_option
@click.option("--timeout", default=15.0, show_default=True, type=float)
@click.option(
    "--log-file", default="./logs/sms-sender.log", show_default=True,
    type=click.Path(dir_okay=False), envvar=LOG_FILE_ENV,
)
def clicks(db_path: str, campaign: str | None, timeout: float, log_file: str) -> None:
    """Fetch click counts from Shlink and show them per segment. Never sends anything.

    Counts exclude bots and link-preview fetchers. Recipients without a user
    ID are shown as missing user ID."""
    logging_config.setup(log_file=log_file, console_level=logging.WARNING)
    _, store, name = _campaign_store(db_path, campaign, changes=True)
    try:
        client = ShlinkClient(load_shlink_config(timeout=timeout))
    except RuntimeError as e:
        raise click.UsageError(str(e)) from e
    try:
        result = sync_clicks(store, client, name)
    except ShlinkError as e:
        click.echo(f"Error: Shlink didn't answer: {e}", err=True)
        sys.exit(2 if isinstance(e, ShlinkHaltError) else 1)
    click.echo(f"{result.links} link(s) at Shlink, {result.clicks} click(s) (bots excluded)")
    segments, campaign_clicks = click_report(store)
    for line in describe_clicks(segments, campaign_clicks):
        click.echo(line)


@cli.command("export-attribution")
@click.option("--state", "db_path", default="./sms_state.db", show_default=True,
              type=click.Path(dir_okay=False))
@_campaign_option
@click.option("--out", default=None, type=click.Path(dir_okay=False),
              help="Default: data/exports/<campaign>-attribution.csv")
def export_attribution(db_path: str, campaign: str | None, out: str | None) -> None:
    """Which recipient each link's `r` belongs to, for the backend. No phone numbers.

    One row per sent recipient with a link of their own: ref, user ID (or
    missing user ID), segment, link, accepted time (ISO 8601, UTC),
    delivery and clicks. Run `clicks` first for fresh counts."""
    _, store, name = _campaign_store(db_path, campaign)
    path = Path(out or f"data/exports/{name}-attribution.csv")
    n = _write_csv(path, ATTRIBUTION_HEADER, attribution_rows(store))
    click.echo(f"Wrote {n} rows to {path}")


@cli.command("export-clickers")
@click.option("--state", "db_path", default="./sms_state.db", show_default=True,
              type=click.Path(dir_okay=False))
@_campaign_option
@click.option("--out", default=None, type=click.Path(dir_okay=False),
              help="Default: data/exports/<campaign>-clickers.csv")
def export_clickers(db_path: str, campaign: str | None, out: str | None) -> None:
    """Recipients who clicked their own link, most clicks first. Contains phone numbers.

    Run `clicks` first for fresh counts."""
    _, store, name = _campaign_store(db_path, campaign)
    path = Path(out or f"data/exports/{name}-clickers.csv")
    n = _write_csv(path, CLICKERS_HEADER, clicker_rows(store))
    click.echo(f"Wrote {n} rows to {path}")


@cli.command("dry-run")
@click.option("--input", "input_path", required=True, type=click.Path(exists=True, dir_okay=False))
@_token_column_option
@_value_map_option
@_user_id_column_option
@_campaign_option
@click.option("--segment", default=None, metavar="NAME",
              help="Segment name for the link preview (default: the file's name).")
@_link_options
def dry_run(
    input_path: str, token_column: tuple[str, ...], value_map: tuple[str, ...],
    user_id_column: str | None, campaign: str | None, segment: str | None,
    link_rate: str, **link_flags: Any,
) -> None:
    """Parse + normalize + dedup, print what would be sent. No API calls.

    With --link-url (and --campaign), also shows the long URL a recipient's
    link would get — nothing is created at Shlink."""
    if segment is not None and not SLUG_RE.match(segment):
        raise click.BadParameter(
            f"{segment!r}: use lowercase letters, digits and dashes", param_hint="--segment",
        )
    token_columns = _parse_token_columns(token_column, value_map, {})
    links = _link_settings(
        **link_flags, campaign=campaign, static_tokens={}, token_columns=token_columns,
    )
    result = _load_input(input_path, token_columns, user_id_column)
    click.echo(f"valid={len(result.valid)} invalid={len(result.invalid)} "
               f"duplicates_collapsed={result.duplicates_collapsed}")
    if user_id_column is not None:
        click.echo(
            f"user IDs: missing user ID={result.missing_user_id} (still sent), "
            f"conflicting user IDs={len(result.conflicts)} phone(s) (not sent)"
        )
    if result.header is not None:
        click.echo(f"  (skipped header row {result.header!r})")
    for r in result.valid[:10]:
        tokens = "".join(f"  {k}={v}" for k, v in r.tokens.items())
        who = (f"  user_id={r.user_id or '(missing user ID)'}"
               if user_id_column is not None else "")
        click.echo(f"  {r.phone}  (raw={r.raw!r}){tokens}{who}")
    if len(result.valid) > 10:
        click.echo(f"  ... and {len(result.valid) - 10} more")
    for inv in result.invalid:
        click.echo(f"  INVALID line {inv.line_no}: {inv.raw!r} — {inv.reason}")
    if links is not None and result.valid:
        assert campaign is not None
        row = plan_link(
            links, campaign=campaign, key=result.valid[0].phone,
            segment=segment or input_loader.segment_from_path(input_path),
        )
        click.echo(
            f"\nlinks: {links.strategy} strategy, {links.token} carries "
            f"{placeholder_token(links.format, shlink_base_url())}"
        )
        click.echo(f"  long URL   {row.long_url}")
        click.echo(f"  title      {row.title}")
        click.echo(f"  tags       {', '.join(row.tags)}")
        click.echo(f"  expires    {row.valid_until}")
        if row.ref:
            click.echo("  (r is random and different for every recipient)")


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
@click.option(
    "--link-token", "link_token", default=None, type=click.Choice(list(TOKEN_MAX_SPACES)),
    help="Show where the campaign's short link will go (a placeholder: links "
         "are only made by `send`).",
)
@click.option("--link-format", "link_format", default="url", show_default=True,
              help="url, code, or a pattern with {code}, as for `send`.")
@click.option("--timeout", default=15.0, show_default=True, type=float)
def preview(
    template: str, token: str | None, token2: str | None, token3: str | None,
    token10: str | None, token20: str | None,
    token_column: tuple[str, ...], value_map: tuple[str, ...],
    phone: str | None, input_path: str | None, limit: int,
    check_account: bool, do_send: bool, link_token: str | None, link_format: str,
    timeout: float,
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
    if do_send and link_token:
        raise click.UsageError(
            "--send can't include a link: links are made by `send`. To see a real "
            "one, use `send --approval-test`, which sends the test SMS its own link."
        )
    if do_send:
        _refuse_while_held(CAMPAIGN_DB_DIR)  # no state DB: the campaigns' folder
    static_tokens = {"token": token, "token2": token2, "token3": token3,
                     "token10": token10, "token20": token20}
    token_columns = _parse_token_columns(token_column, value_map, static_tokens)
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
    if link_token is not None:
        problem = link_format_problem(link_format)
        if problem:
            raise click.BadParameter(problem, param_hint="--link-format")
        if static_tokens[link_token] is not None or (
            token_columns is not None and link_token in token_columns.columns
        ):
            raise click.UsageError(f"{link_token} carries the link; don't also set it")
        placeholder = placeholder_token(link_format, shlink_base_url())
        row_tokens = {p: {**row_tokens.get(p, {}), link_token: placeholder} for p in phones}
    for p in phones:
        params = sender.build_params(p, row_tokens.get(p))
        click.echo(f"\nPOST {base_url}")
        for k, v in params.items():
            click.echo(f"  {k}={v}")

    if do_send:
        if sender.allowlist is not None and not sender.allowlist.allows(phones[0]):
            click.echo(
                f"Error: restricted sending: {phones[0]} isn't on {ENV_ALLOWED_NUMBERS} "
                "(or it holds a value that isn't a phone number). Nothing was sent.",
                err=True,
            )
            sys.exit(2)
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
