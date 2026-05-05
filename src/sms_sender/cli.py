"""Command-line interface."""
from __future__ import annotations

import csv
import logging
import sys
from pathlib import Path

import click

from . import logging_config
from .config import load_api_key
from .runner import make_runner
from .sender import SenderConfig
from .state import StateStore

logger = logging.getLogger(__name__)


@click.group()
def cli() -> None:
    """Reliable bulk SMS sender for Kavenegar verify/lookup."""


@cli.command()
@click.option(
    "--input", "input_path", required=True, type=click.Path(exists=True, dir_okay=False),
    help="Path to a .txt (one number per line) or .csv (first cell per row).",
)
@click.option("--template", required=True, help="Kavenegar template name.")
@click.option("--token", default=None, help="Static token value (no spaces).")
@click.option("--token2", default=None, help="Static token2 value (no spaces).")
@click.option("--token3", default=None, help="Static token3 value (no spaces).")
@click.option("--token10", default=None, help="Static token10 value (allows up to 5 spaces).")
@click.option("--token20", default=None, help="Static token20 value (allows up to 8 spaces).")
@click.option("--workers", default=5, show_default=True, type=int)
@click.option("--max-attempts", default=5, show_default=True, type=int)
@click.option("--timeout", default=15.0, show_default=True, type=float)
@click.option("--backoff-max", default=30.0, show_default=True, type=float)
@click.option(
    "--state", "db_path", default="./sms_state.db", show_default=True,
    type=click.Path(dir_okay=False),
    help="SQLite file used for resume + dedup. Re-runs reuse the same DB.",
)
@click.option(
    "--log-file", default="./logs/sms-sender.log", show_default=True,
    type=click.Path(dir_okay=False),
)
@click.option("--verbose", is_flag=True, help="DEBUG-level console output.")
@click.option("--quiet", is_flag=True, help="Only WARNING+ on console.")
def send(
    input_path: str, template: str, token: str | None, token2: str | None,
    token3: str | None, token10: str | None, token20: str | None,
    workers: int, max_attempts: int, timeout: float,
    backoff_max: float, db_path: str, log_file: str, verbose: bool, quiet: bool,
) -> None:
    """Send SMS to every number in INPUT, resuming from prior state."""
    if verbose and quiet:
        raise click.UsageError("--verbose and --quiet are mutually exclusive")
    console_level = (
        logging.DEBUG if verbose else (logging.WARNING if quiet else logging.INFO)
    )
    logging_config.setup(log_file=log_file, console_level=console_level)

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
    )
    summary = runner.run()

    click.echo(
        f"\nSummary: sent={summary.sent} "
        f"failed_permanent={summary.failed_permanent} "
        f"failed_retriable={summary.failed_retriable} "
        f"invalid={summary.invalid} "
        f"halted={summary.halted}"
    )
    if summary.halted:
        sys.exit(2)
    if summary.failed_permanent or summary.failed_retriable:
        sys.exit(1)


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


if __name__ == "__main__":
    cli()
