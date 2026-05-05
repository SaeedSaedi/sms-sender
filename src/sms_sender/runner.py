"""Orchestrate: load input → seed state → fan out workers → progress + log."""
from __future__ import annotations

import logging
import signal
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path

from tqdm import tqdm

from . import input_loader
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


class Runner:
    def __init__(
        self,
        *,
        input_path: str | Path,
        state: StateStore,
        sender: Sender,
        workers: int = 5,
    ):
        self.input_path = Path(input_path)
        self.state = state
        self.sender = sender
        self.workers = workers
        self._stop = threading.Event()

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

        # 4. Fan out.
        sent = failed_permanent = failed_retriable = 0
        halted = False
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
                            bar.set_postfix(
                                sent=sent, fail=failed_permanent + failed_retriable
                            )
                    finally:
                        if halted or self._stop.is_set():
                            for f in futures:
                                f.cancel()

        summary = RunSummary(
            total_input=len(loaded.valid) + len(loaded.invalid),
            new_recipients=new_count,
            duplicates_collapsed=loaded.duplicates_collapsed,
            invalid=len(loaded.invalid),
            sent=sent,
            failed_permanent=failed_permanent,
            failed_retriable=failed_retriable,
            halted=halted,
        )
        logger.info("run_end", extra=summary.__dict__)
        return summary


def make_runner(
    *,
    input_path: str | Path,
    db_path: str | Path,
    sender_cfg: SenderConfig,
    workers: int = 5,
) -> Runner:
    state = StateStore(db_path)
    sender = Sender(sender_cfg)
    return Runner(input_path=input_path, state=state, sender=sender, workers=workers)
