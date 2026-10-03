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
from .input_loader import TokenColumns
from .locking import RunLock
from .rate import TokenBucket
from .reconcile import DEFAULT_MIN_AGE_SEC, REQUEUE_NOT_FOUND, reconcile_unknown
from .redact import redact_secrets
from .sender import (
    Attempt,
    HaltError,
    PermanentSendError,
    SendError,
    Sender,
    SenderConfig,
    UncertainSendError,
)
from .state import NEEDS_REVIEW, UNKNOWN, StateStore

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
    # Rows still `unknown` in the DB when the run ended — this run's and
    # earlier ones'. They may have been sent, so they are never resent blindly.
    unknown: int = 0
    # Rows reconciliation couldn't settle on its own (an operator decides).
    needs_review: int = 0


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
        token_columns: TokenColumns | None = None,
        reconcile_min_age_sec: float = DEFAULT_MIN_AGE_SEC,
        reconcile_requeue_not_found: bool = REQUEUE_NOT_FOUND,
    ):
        self.input_path = Path(input_path)
        self.reconcile_min_age_sec = reconcile_min_age_sec
        self.reconcile_requeue_not_found = reconcile_requeue_not_found
        self.state = state
        self.sender = sender
        self.workers = workers
        self.preflight = preflight
        self.smoke_test = smoke_test
        self.approval_test_number = approval_test_number
        self._approval_prompt: ApprovalPrompt = approval_prompt or _click_approval_prompt
        self.token_columns = token_columns
        # phone → per-recipient tokens, filled from the input by `run()`.
        # None means every recipient gets the static tokens in SenderConfig.
        self._row_tokens: dict[str, dict[str, str]] | None = None
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
        tokens = self._row_tokens.get(phone) if self._row_tokens is not None else None
        try:
            result = self.sender.send(phone, tokens=tokens)
        except HaltError as e:
            self.state.mark_failed(phone, e.status_code, e.message, permanent=False)
            self._record_error(e.status_code, e.message)
            logger.error(
                "halt",
                extra={"phone": phone, "status": e.status_code, "detail": e.message},
            )
            self._stop.set()
            raise
        except UncertainSendError as e:
            # It may have been accepted: park it as `unknown`, never retry it here.
            self.state.mark_unknown(phone, e.message)
            self._record_error(e.status_code, e.message)
            logger.warning(
                "send_outcome_unknown",
                extra={"phone": phone, "detail": e.message, "attempts": recipient.attempts},
            )
            return ("unknown", phone)
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
            self.state.mark_sent(phone, result.message_id, result.status_code, result.cost)
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
        tokens = None
        if self._row_tokens is not None:
            # Per-recipient tokens: show the operator a real recipient's
            # message — their own row if they're in the file, else the first.
            sample = target if target in self._row_tokens else (phones[0] if phones else None)
            if sample is None:
                raise PreflightError("approval test needs a recipient row to borrow tokens from")
            tokens = self._row_tokens[sample]
            tqdm.write(f"Approval test uses the tokens of {sample}.")
        logger.info("approval_test_target", extra={"phone": target})
        tqdm.write(f"Approval test: sending to {target} synchronously …")

        try:
            result = self.sender.send(target, tokens=tokens)
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

    def _reconcile_unknown(self, when: str) -> None:
        """Settle `unknown` rows that are old enough, by asking Kavenegar.

        Best-effort: when the lookup can't run (fake sender, network, account
        problem) the rows simply stay `unknown` — never claimable — so this
        can't cause a double send.
        """
        if not hasattr(self.sender, "find_messages"):
            return  # test fakes / alternate senders
        try:
            result = reconcile_unknown(
                self.state, self.sender, min_age_sec=self.reconcile_min_age_sec,
                requeue_not_found=self.reconcile_requeue_not_found,
            )
        except SendError as e:  # incl. HaltError: bad key, account problem
            logger.warning(
                "reconcile_skipped",
                extra={"when": when, "status": e.status_code, "detail": e.message},
            )
            return
        if result.checked:
            tqdm.write(
                f"Checked {result.checked} unknown row(s) with Kavenegar: "
                f"{result.sent} had been sent, {result.requeued} had not (safe to "
                f"send again), {result.needs_review} need review."
            )
        if result.checked or result.deferred:
            logger.info(
                "reconcile_done",
                extra={
                    "when": when, "sent": result.sent, "requeued": result.requeued,
                    "needs_review": result.needs_review, "deferred": result.deferred,
                },
            )

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
        # One process per DB: a second run would treat our `in_flight` rows
        # as orphans and send them again. Raises RunLockError if taken.
        with RunLock(self.state.db_path):
            return self._run()

    def _run(self) -> RunSummary:
        self._install_signal_handlers()

        # 1. Load input.
        loaded = input_loader.load(self.input_path, self.token_columns)
        if self.token_columns is not None:
            self._row_tokens = {r.phone: r.tokens for r in loaded.valid}
        logger.info(
            "input_loaded",
            extra={
                "path": str(self.input_path),
                "valid": len(loaded.valid),
                "invalid": len(loaded.invalid),
                "duplicates_collapsed": loaded.duplicates_collapsed,
                "header": loaded.header,
            },
        )

        # 2. Seed state.
        if loaded.invalid:
            self.state.record_invalid_many(
                [(inv.raw, inv.reason) for inv in loaded.invalid]
            )
        new_count = self.state.upsert_pending([(r.phone, r.raw) for r in loaded.valid])
        orphans = self.state.mark_orphans_unknown()
        if orphans:
            logger.warning("orphans_marked_unknown", extra={"n": orphans})
            tqdm.write(
                f"{orphans} recipient(s) were mid-send when the last run stopped. They are "
                "marked unknown and NOT resent: they may already have the SMS."
            )
        # Settle old-enough `unknown` rows first, so the ones Kavenegar never
        # got go out in this run like everyone else.
        self._reconcile_unknown("start")

        # 3. Snapshot work to do.
        phones = self.state.list_claimable_phones()
        if self._row_tokens is not None:
            # Per-recipient tokens live in the input file, so a claimable row
            # left in the DB by a different input has nothing to send — leave it.
            not_in_input = sum(1 for p in phones if p not in self._row_tokens)
            if not_in_input:
                logger.warning("skipped_not_in_input", extra={"n": not_in_input})
                tqdm.write(
                    f"Skipping {not_in_input} claimable row(s) that aren't in "
                    f"{self.input_path.name} (no per-recipient tokens for them)."
                )
                phones = [p for p in phones if p in self._row_tokens]
        logger.info(
            "run_start",
            extra={
                "to_send": len(phones),
                "new": new_count,
                "workers": self.workers,
                # Campaign history: which template this run sent (fakes have no cfg).
                "template": getattr(getattr(self.sender, "cfg", None), "template", None),
            },
        )

        sent = failed_permanent = failed_retriable = already_done = unknown_seen = 0
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
                            elif outcome == "unknown":
                                unknown_seen += 1
                            elif outcome in ("already_done", "skipped"):
                                already_done += 1
                            bar.update(1)
                            processed = sent + failed_permanent + failed_retriable + unknown_seen
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

        # On a long run, rows that went `unknown` early may be old enough now.
        if not halted and not self._stop.is_set():
            self._reconcile_unknown("end")

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
        counts = self.state.counts()
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
            unknown=counts.get(UNKNOWN, 0),
            needs_review=counts.get(NEEDS_REVIEW, 0),
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
        "unknown": s.unknown,
        "needs_review": s.needs_review,
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
    if s.unknown:
        lines.append(f"  unknown           {s.unknown}  (may have been sent; never resent blindly)")
    if s.needs_review:
        lines.append(f"  needs_review      {s.needs_review}  (Kavenegar check was ambiguous)")
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


def _attempt_recorder(state: StateStore) -> Callable[[Attempt], None]:
    """Write each call to Kavenegar into the state DB's `attempts` table."""
    def record(a: Attempt) -> None:
        state.record_attempt(
            phone=a.phone, kind="send", outcome=a.outcome,
            started_at=a.started_at, finished_at=a.finished_at,
            status_code=a.status_code, message_id=a.message_id, cost=a.cost,
            detail=a.detail,
        )
    return record


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
    token_columns: TokenColumns | None = None,
) -> Runner:
    state = StateStore(db_path)
    sender = Sender(sender_cfg, on_attempt=_attempt_recorder(state))
    return Runner(
        input_path=input_path,
        state=state,
        sender=sender,
        workers=workers,
        preflight=preflight,
        smoke_test=smoke_test,
        rate_per_sec=rate_per_sec,
        approval_test_number=approval_test_number,
        token_columns=token_columns,
    )
