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

from tqdm import tqdm

from . import input_loader
from .rate import TokenBucket
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


class PreflightError(Exception):
    """Preflight check failed — abort before fanning out."""


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
    ):
        self.input_path = Path(input_path)
        self.state = state
        self.sender = sender
        self.workers = workers
        self.preflight = preflight
        self.smoke_test = smoke_test
        self._bucket = TokenBucket(rate_per_sec)
        self._stop = threading.Event()
        self._error_counter: Counter[str] = Counter()
        self._error_lock = threading.Lock()

    def _record_error(self, status_code: int | None, message: str) -> None:
        """Bucket failures by `[code] message` for the end-of-run report."""
        key = f"[{status_code if status_code is not None else 'net'}] {message}"
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
        """Account check + optional smoke send. Raises PreflightError on failure.

        - account_info: catches bad API key / disabled account before fan-out.
        - smoke_test: sends synchronously to the first claimable phone. If it
          raises HaltError or PermanentSendError, we abort instead of burning
          credit on a wrong template across N workers.
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
        else:
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

        if not self.smoke_test or not phones:
            return

        target = phones[0]
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
        for inv in loaded.invalid:
            self.state.record_invalid(inv.raw, inv.reason)
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

        sent = failed_permanent = failed_retriable = 0
        halted = False
        started = time.monotonic()

        # 4. Preflight (account check + optional smoke send to phones[0]).
        try:
            self._preflight(phones)
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
            failed_retriable=failed_retriable, halted=halted,
            started=started,
        )
        logger.info("run_end", extra=_summary_log_fields(summary))
        return summary

    def _build_summary(
        self, *, loaded, new_count: int,
        sent: int, failed_permanent: int, failed_retriable: int,
        halted: bool, started: float,
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
        )


def _summary_log_fields(s: RunSummary) -> dict:
    """Flatten a RunSummary for the structured logger (no tuple/list values)."""
    return {
        "total_input": s.total_input,
        "new_recipients": s.new_recipients,
        "duplicates_collapsed": s.duplicates_collapsed,
        "invalid": s.invalid,
        "sent": s.sent,
        "failed_permanent": s.failed_permanent,
        "failed_retriable": s.failed_retriable,
        "halted": s.halted,
        "elapsed_sec": s.elapsed_sec,
        "sends_per_sec": s.sends_per_sec,
        "top_error": s.top_errors[0][0] if s.top_errors else None,
    }


def format_report(s: RunSummary) -> str:
    """Render a human-readable end-of-run report."""
    lines = [
        "─" * 60,
        f"Run summary",
        "─" * 60,
        f"  sent              {s.sent}",
        f"  failed_permanent  {s.failed_permanent}",
        f"  failed_retriable  {s.failed_retriable}",
        f"  invalid           {s.invalid}",
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
    )
