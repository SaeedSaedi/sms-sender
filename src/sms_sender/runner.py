"""Orchestrate: load input → seed state → fan out workers → progress + log."""
from __future__ import annotations

import logging
import signal
import threading
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from tqdm import tqdm

from . import input_loader
from .rate import TokenBucket
from .redact import redact_secrets
from .sender import HaltError, PermanentSendError, SendError, Sender, SenderConfig
from .state import StateStore

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class RunSummary:
    total_input: int
    new_recipients: int
    duplicates_collapsed: int
    invalid: int
    sent: int
    failed_permanent: int
    failed_retriable: int
    halted: bool
    elapsed_sec: float = 0.0
    sends_per_sec: float = 0.0
    top_errors: tuple[tuple[str, int], ...] = field(default_factory=tuple)
    # Recipients that were claimed by a peer worker, were already `sent`, or
    # otherwise lost the claim race. Useful for diagnosing resume runs.
    already_done: int = 0


class PreflightError(Exception):
    """Preflight check failed — abort before fanning out."""


def _click_approval_prompt(test_number: str, recipient_count: int) -> bool:
    """Default human-in-the-loop gate after a successful approval-test SMS.

    Reads from stdin via Click. EOF / Ctrl-C / closed stdin (CI, piped) all
    raise click.Abort, which we treat as 'declined' so the run aborts safely
    instead of hanging or proceeding without confirmation.
    """
    import click
    try:
        return click.confirm(
            f"\nTest SMS sent to {test_number}. Did you receive it correctly? "
            f"Proceed with {recipient_count} recipient(s)?",
            default=False,
        )
    except click.Abort:
        return False


ApprovalPrompt = Callable[[str, int], bool]


class Runner:
    def __init__(
        self,
        *,
        input_path: str | Path,
        state: StateStore,
        sender: Sender,
        workers: int = 5,
        preflight: bool = True,
        smoke_test: bool = False,
        rate_per_sec: float = 0.0,
        approval_test_number: str | None = None,
        approval_prompt: ApprovalPrompt | None = None,
    ):
        self.input_path = Path(input_path)
        self.state = state
        self.sender = sender
        self.workers = workers
        self.preflight = preflight
        self.smoke_test = smoke_test
        self.approval_test_number = approval_test_number
        self._approval_prompt: ApprovalPrompt = approval_prompt or _click_approval_prompt
        self._bucket = TokenBucket(rate_per_sec)
        self._stop = threading.Event()
        self._error_counter: Counter[str] = Counter()
        self._error_lock = threading.Lock()

    def _record_error(self, status_code: int | None, message: str) -> None:
        """Bucket failures by `[code] message` for the end-of-run report."""
        safe = redact_secrets(message)
        key = f"[{status_code if status_code is not None else 'net'}] {safe}"
        with self._error_lock:
            self._error_counter[key] += 1

    def _install_signal_handlers(self) -> None:
        def handler(signum, _frame):
            if self._stop.is_set():
                logger.warning("force_exit", extra={"signal": signum})
                raise KeyboardInterrupt
            logger.warning(
                "graceful_shutdown_requested",
                extra={"signal": signum, "hint": "press_again_to_force"},
            )
            self._stop.set()

        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                signal.signal(sig, handler)
            except ValueError:
                # Not on the main thread (e.g., tests) — skip.
                pass

    def _send_one(self, phone: str) -> tuple[str, str]:
        if self._stop.is_set():
            return ("skipped", phone)
        # Rate-limit BEFORE claim so we don't lock the row while sleeping.
        self._bucket.acquire()
        if self._stop.is_set():
            return ("skipped", phone)
        recipient = self.state.claim(phone)
        if recipient is None:
            # Already sent or claimed by someone else.
            return ("already_done", phone)
        try:
            result = self.sender.send(phone)
        except HaltError as e:
            self.state.mark_failed(phone, e.status_code, e.message, permanent=False)
            self._record_error(e.status_code, e.message)
            logger.error(
                "halt",
                extra={"phone": phone, "status": e.status_code, "detail": e.message},
            )
            self._stop.set()
            raise
        except PermanentSendError as e:
            self.state.mark_failed(phone, e.status_code, e.message, permanent=True)
            self._record_error(e.status_code, e.message)
            logger.error(
                "send_failed_permanent",
                extra={
                    "phone": phone,
                    "status": e.status_code,
                    "detail": e.message,
                    "attempts": recipient.attempts,
                },
            )
            return ("failed_permanent", phone)
        except SendError as e:
            self.state.mark_failed(phone, e.status_code, e.message, permanent=False)
            self._record_error(e.status_code, e.message)
            logger.warning(
                "send_failed_retriable",
                extra={
                    "phone": phone,
                    "status": e.status_code,
                    "detail": e.message,
                    "attempts": recipient.attempts,
                },
            )
            return ("failed_retriable", phone)
        else:
            self.state.mark_sent(phone, result.message_id, result.status_code)
            logger.info(
                "send_ok",
                extra={
                    "phone": phone,
                    "msg_id": result.message_id,
                    "attempts": recipient.attempts,
                },
            )
            return ("sent", phone)

    def _preflight(self, phones: list[str]) -> None:
        """Account info check. Raises PreflightError on auth/quota failures.

        Catches bad API key / disabled account / zero credit before fan-out.
        Network blips on this endpoint are logged and ignored — the actual
        sends will surface those if they persist.
        """
        if not self.preflight:
            return

        if not hasattr(self.sender, "account_info"):
            # Test fakes / alternate senders may not implement it. Soft-skip.
            return

        try:
            info = self.sender.account_info()
        except HaltError as e:
            logger.error(
                "preflight_halt",
                extra={"status": e.status_code, "detail": e.message},
            )
            raise PreflightError(f"account check failed: [{e.status_code}] {e.message}") from e
        except SendError as e:
            # Network blip on the account endpoint — non-fatal, log and move on.
            logger.warning("preflight_account_unreachable", extra={"detail": e.message})
            return

        logger.info(
            "preflight_account_ok",
            extra={
                "remaining_credit": info.remaining_credit,
                "expire_date": info.expire_date,
                "type": info.type,
            },
        )
        if info.remaining_credit is not None:
            tqdm.write(
                f"Account: credit={info.remaining_credit} "
                f"expires={info.expire_date or '?'} type={info.type or '?'}"
            )
            if phones and info.remaining_credit <= 0:
                raise PreflightError(
                    f"remaining credit is {info.remaining_credit}; top up before sending"
                )

    def _approval_test(self, phones: list[str]) -> None:
        """Send to the operator's own number out-of-band, then prompt y/N.

        The send bypasses the state DB, the rate limiter, and the executor —
        same model as `account_info`. It runs through `Sender.send`, so it
        gets the normal retry policy and error classification.

        On any send failure (Halt, Permanent, retries-exhausted) the run
        aborts before the prompt — there's no point asking the operator to
        approve something that didn't reach them. The approval prompt also
        treats a closed stdin / EOF as 'declined' so CI is safe.
        """
        if not self.approval_test_number:
            return

        target = self.approval_test_number
        logger.info("approval_test_target", extra={"phone": target})
        tqdm.write(f"Approval test: sending to {target} synchronously …")

        try:
            result = self.sender.send(target)
        except HaltError as e:
            self._record_error(e.status_code, e.message)
            raise PreflightError(
                f"approval test send to {target} halted: [{e.status_code}] {e.message}"
            ) from e
        except SendError as e:
            # PermanentSendError or retries-exhausted SendError — both fatal here.
            self._record_error(e.status_code, e.message)
            raise PreflightError(
                f"approval test send to {target} failed: [{e.status_code}] {e.message}"
            ) from e

        logger.info(
            "approval_test_sent",
            extra={"phone": target, "msg_id": result.message_id},
        )
        approved = self._approval_prompt(target, len(phones))
        if not approved:
            logger.warning("approval_declined", extra={"phone": target})
            raise PreflightError("approval declined by operator; aborting before fan-out")
        logger.info("approval_granted", extra={"phone": target})
        tqdm.write("Approval granted.")

    def _smoke_test_run(self, phones: list[str]) -> None:
        """Synchronous send to phones[0]. Raises PreflightError on non-success.

        Gated by `self.smoke_test` AND `self.preflight` (preserves prior
        behavior — `--no-preflight` disables this too). If it raises HaltError
        or PermanentSendError, we abort instead of burning credit on a wrong
        template across N workers.
        """
        if not (self.smoke_test and self.preflight) or not phones:
            return

        target = phones[0]
        logger.info("smoke_test_target", extra={"phone": target})
        tqdm.write(f"Smoke test: sending to {target} synchronously …")
        outcome, _ = self._send_one(target)
        if outcome != "sent":
            raise PreflightError(
                f"smoke test to {target} did not succeed (outcome={outcome}); "
                "fix the issue (template/tokens/account) before sending the rest"
            )
        tqdm.write("Smoke test passed.")

    def run(self) -> RunSummary:
        self._install_signal_handlers()

        # 1. Load input.
        loaded = input_loader.load(self.input_path)
        logger.info(
            "input_loaded",
            extra={
                "path": str(self.input_path),
                "valid": len(loaded.valid),
                "invalid": len(loaded.invalid),
                "duplicates_collapsed": loaded.duplicates_collapsed,
            },
        )

        # 2. Seed state.
        if loaded.invalid:
            self.state.record_invalid_many(
                [(inv.raw, inv.reason) for inv in loaded.invalid]
            )
        new_count = self.state.upsert_pending([(r.phone, r.raw) for r in loaded.valid])
        reclaimed = self.state.reset_orphan_in_flight()
        if reclaimed:
            logger.warning("reclaimed_orphan_in_flight", extra={"n": reclaimed})

        # 3. Snapshot work to do.
        phones = self.state.list_claimable_phones()
        logger.info(
            "run_start",
            extra={
                "to_send": len(phones),
                "new": new_count,
                "workers": self.workers,
            },
        )

        sent = failed_permanent = failed_retriable = already_done = 0
        halted = False
        started = time.monotonic()

        # 4. Preflight: account check → optional approval-test (manual gate)
        #    → optional smoke-test (auto). Any failure aborts before fan-out.
        try:
            self._preflight(phones)
            self._approval_test(phones)
            self._smoke_test_run(phones)
        except PreflightError as e:
            logger.error("preflight_failed", extra={"detail": str(e)})
            tqdm.write(f"Preflight failed: {e}")
            return self._build_summary(
                loaded=loaded, new_count=new_count,
                sent=sent, failed_permanent=failed_permanent,
                failed_retriable=failed_retriable, halted=True,
                started=started,
            )

        # The smoke test consumed phones[0] synchronously — refresh the queue.
        if self.smoke_test and self.preflight and phones:
            smoked = phones[0]
            phones = [p for p in phones if p != smoked]
            sent += 1  # the smoke send already succeeded by this point

        # 5. Fan out.
        if phones:
            with tqdm(total=len(phones), unit="sms", dynamic_ncols=True) as bar:
                with ThreadPoolExecutor(max_workers=self.workers) as ex:
                    futures = {ex.submit(self._send_one, p): p for p in phones}
                    try:
                        for fut in as_completed(futures):
                            try:
                                outcome, _ = fut.result()
                            except HaltError:
                                halted = True
                                break
                            if outcome == "sent":
                                sent += 1
                            elif outcome == "failed_permanent":
                                failed_permanent += 1
                            elif outcome == "failed_retriable":
                                failed_retriable += 1
                            elif outcome in ("already_done", "skipped"):
                                already_done += 1
                            bar.update(1)
                            processed = sent + failed_permanent + failed_retriable
                            ok_pct = (sent / processed * 100.0) if processed else 0.0
                            bar.set_postfix(
                                sent=sent,
                                fail=failed_permanent + failed_retriable,
                                ok=f"{ok_pct:.0f}%",
                            )
                    finally:
                        if halted or self._stop.is_set():
                            for f in futures:
                                f.cancel()

        summary = self._build_summary(
            loaded=loaded, new_count=new_count,
            sent=sent, failed_permanent=failed_permanent,
            failed_retriable=failed_retriable, already_done=already_done,
            halted=halted, started=started,
        )
        logger.info("run_end", extra=_summary_log_fields(summary))
        return summary

    def _build_summary(
        self, *, loaded, new_count: int,
        sent: int, failed_permanent: int, failed_retriable: int,
        already_done: int = 0, halted: bool, started: float,
    ) -> RunSummary:
        elapsed = max(0.0, time.monotonic() - started)
        rate = (sent / elapsed) if elapsed > 0 else 0.0
        with self._error_lock:
            top = tuple(self._error_counter.most_common(3))
        return RunSummary(
            total_input=len(loaded.valid) + len(loaded.invalid),
            new_recipients=new_count,
            duplicates_collapsed=loaded.duplicates_collapsed,
            invalid=len(loaded.invalid),
            sent=sent,
            failed_permanent=failed_permanent,
            failed_retriable=failed_retriable,
            halted=halted,
            elapsed_sec=round(elapsed, 3),
            sends_per_sec=round(rate, 3),
            top_errors=top,
            already_done=already_done,
        )


def _summary_log_fields(s: RunSummary) -> dict:
    """Flatten a RunSummary for the structured logger (no tuple/list values)."""
    # Join up to the top 3 buckets so all of them appear in one greppable line.
    top = " | ".join(f"{m} (x{c})" for m, c in s.top_errors) if s.top_errors else None
    return {
        "total_input": s.total_input,
        "new_recipients": s.new_recipients,
        "duplicates_collapsed": s.duplicates_collapsed,
        "invalid": s.invalid,
        "sent": s.sent,
        "failed_permanent": s.failed_permanent,
        "failed_retriable": s.failed_retriable,
        "already_done": s.already_done,
        "halted": s.halted,
        "elapsed_sec": s.elapsed_sec,
        "sends_per_sec": s.sends_per_sec,
        "top_errors": top,
    }


def format_report(s: RunSummary) -> str:
    """Render a human-readable end-of-run report."""
    lines = [
        "─" * 60,
        "Run summary",
        "─" * 60,
        f"  sent              {s.sent}",
        f"  failed_permanent  {s.failed_permanent}",
        f"  failed_retriable  {s.failed_retriable}",
        f"  invalid           {s.invalid}",
    ]
    if s.already_done:
        lines.append(f"  already_done      {s.already_done}")
    lines += [
        f"  halted            {s.halted}",
        f"  elapsed           {s.elapsed_sec:.2f}s ({s.sends_per_sec:.2f} sends/sec)",
    ]
    if s.top_errors:
        lines.append("  top errors:")
        for msg, count in s.top_errors:
            lines.append(f"    {count:>4}  {msg}")
    lines.append("─" * 60)
    return "\n".join(lines)


def make_runner(
    *,
    input_path: str | Path,
    db_path: str | Path,
    sender_cfg: SenderConfig,
    workers: int = 5,
    preflight: bool = True,
    smoke_test: bool = False,
    rate_per_sec: float = 0.0,
    approval_test_number: str | None = None,
) -> Runner:
    state = StateStore(db_path)
    sender = Sender(sender_cfg)
    return Runner(
        input_path=input_path,
        state=state,
        sender=sender,
        workers=workers,
        preflight=preflight,
        smoke_test=smoke_test,
        rate_per_sec=rate_per_sec,
        approval_test_number=approval_test_number,
    )
