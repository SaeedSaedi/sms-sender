"""Orchestrate a run: load input → seed state → preflight → fan out → report.

`run()` goes through three stages — `_prepare`, `_preflight_checks`,
`_fan_out` — and is built to be driven without a terminal: progress goes
to a `Reporter` (the CLI's draws the tqdm bar), `cancel()` stops it from
another thread, and the Ctrl-C / SIGTERM handlers are optional and restored
afterwards.
"""
from __future__ import annotations

import json
import logging
import signal
import threading
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable, Iterator, Protocol, Sequence

from tqdm import tqdm

from . import input_loader
from .allowlist import ENV_ALLOWED_NUMBERS
from .input_loader import InputPart, LoadResult, TokenColumns
from .links import DEFAULT_WORKERS as DEFAULT_LINK_WORKERS
from .links import LinkClient, LinkError, LinkSettings, LinkStage
from .locking import RunLock
from .rate import TokenBucket
from .reconcile import DEFAULT_MIN_AGE_SEC, REQUEUE_NOT_FOUND, reconcile_unknown
from .redact import redact_secrets
from .sender import (
    TOKEN_MAX_SPACES,
    Attempt,
    HaltError,
    PermanentSendError,
    SendError,
    Sender,
    SenderConfig,
    UncertainSendError,
)
from .shortlink import ShlinkHaltError
from .frequency import FrequencyCap
from .state import CAPPED, NEEDS_REVIEW, SUPPRESSED, UNKNOWN, StateStore, folder_sends_since
from .window import TEHRAN, SendWindow, now_tehran

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
    # Ctrl-C, SIGTERM or `cancel()` stopped the run before it finished;
    # re-running the same command continues where it stopped.
    stopped: bool = False
    # Rows on the opt-out list in this campaign's DB (never sent).
    suppressed: int = 0
    capped: int = 0       # over the frequency cap, held back this run
    # What this run's accepted SMS cost, in rials (Kavenegar's own figures,
    # approval test included).
    cost: int = 0
    # With a user-ID column: input rows whose user ID was blank (still sent,
    # reported as "missing user ID"), and phones excluded because they came
    # with two different user IDs.
    missing_user_id: int = 0
    user_id_conflicts: int = 0
    # Short links this run's SMS carry, and how many it had to create.
    links_ready: int = 0
    links_created: int = 0
    # The pre-send figures: the account's credit, the cost of one SMS (from
    # the approval test, or what the campaign already paid), and the
    # estimate for the recipients still to send. None while unknown.
    credit: int | None = None
    cost_per_sms: int | None = None
    estimate: int | None = None
    # The approval test's message ID, when one was sent.
    test_message_id: int | None = None
    # A `test_only` run: everything up to the test SMS, and no sending.
    test_only: bool = False
    # Why the run halted or stopped early, as a key plus its numbers (see
    # PreflightError); None when it ran to the end or the operator stopped it.
    stop_reason: str | None = None
    stop_fields: dict = field(default_factory=dict)


class PreflightError(Exception):
    """Preflight check failed — abort before fanning out.

    `key` names the reason and `fields` holds its numbers, so the dashboard
    can say it in Persian (spec 4.11: engine text is English, for the CLI
    and the logs; the dashboard renders keys)."""

    def __init__(self, message: str, key: str = "preflight", **fields):
        super().__init__(message)
        self.key = key
        self.fields = fields


@dataclass
class SendCounts:
    """Running tallies of one fan-out, handed to the Reporter as it goes."""
    total: int = 0
    sent: int = 0
    failed_permanent: int = 0
    failed_retriable: int = 0
    unknown: int = 0
    already_done: int = 0

    @property
    def processed(self) -> int:
        return self.sent + self.failed_permanent + self.failed_retriable + self.unknown


class Reporter(Protocol):
    """Where a run reports progress: notes for the operator, and a tick per
    finished recipient. The dashboard worker passes one that writes its DB.

    A note is English text for the CLI, plus a `key` and its `fields`, so
    the dashboard can say it in Persian (never the engine's English)."""

    def note(self, text: str, key: str = "note", **fields: object) -> None: ...
    def start(self, total: int) -> None: ...
    def advance(self, counts: SendCounts) -> None: ...
    def finish(self) -> None: ...


class TqdmReporter:
    """The CLI's progress bar and notes — what the tool has always printed."""

    def __init__(self) -> None:
        self._bar: tqdm | None = None

    def note(self, text: str, key: str = "note", **fields: object) -> None:
        tqdm.write(text)

    def start(self, total: int) -> None:
        self._bar = tqdm(total=total, unit="sms", dynamic_ncols=True)

    def advance(self, counts: SendCounts) -> None:
        if self._bar is None:
            return
        self._bar.update(1)
        ok_pct = (counts.sent / counts.processed * 100.0) if counts.processed else 0.0
        self._bar.set_postfix(
            sent=counts.sent,
            fail=counts.failed_permanent + counts.failed_retriable,
            ok=f"{ok_pct:.0f}%",
        )

    def finish(self) -> None:
        if self._bar is not None:
            self._bar.close()
            self._bar = None


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
        reporter: Reporter | None = None,
        install_signal_handlers: bool = True,
        campaign: str | None = None,
        settings: dict | None = None,
        allow_settings_change: bool = False,
        opt_out: frozenset[str] | None = None,
        frequency_cap: FrequencyCap | None = None,
        send_window: SendWindow | None = None,
        clock: Callable[[], datetime] = now_tehran,
        user_id_column: str | None = None,
        segment: str | None = None,
        more_inputs: Sequence[InputPart] = (),
        links: LinkSettings | None = None,
        link_client: LinkClient | None = None,
        link_workers: int = DEFAULT_LINK_WORKERS,
        link_rate_per_sec: float = 10.0,
        test_only: bool = False,
        cost_per_sms: int | None = None,
    ):
        if links is not None and (link_client is None or not campaign):
            raise ValueError("links need a link client and a campaign name")
        if test_only and not approval_test_number:
            raise ValueError("a test-only run needs an approval test number")
        # The dashboard's test step: validate, links and pre-send checks,
        # the test SMS to the operator, then stop. The operator approves it
        # in the browser, and a later run sends, given this test's cost per
        # SMS (`cost_per_sms`) for the credit estimate.
        self.test_only = test_only
        # Short links (Phase 2): created for every recipient before any SMS.
        self.links = links
        self.link_client = link_client
        self.link_workers = link_workers
        self.link_rate_per_sec = link_rate_per_sec
        self._link_tokens: dict[str, str] | None = None  # phone → link token value
        self._link_keys: dict[str, str] = {}             # phone → link key, set at claim
        self._test_link_token: str | None = None
        self._links_ready = 0
        self._links_created = 0
        self.input_path = Path(input_path)
        # Which input column holds each recipient's user ID (None: no IDs),
        # and the segment this input is (default: the file's name).
        self.user_id_column = user_id_column
        self.segment = segment or input_loader.segment_from_path(input_path)
        # A send may read several lists, in order (the dashboard's segments):
        # this one, then `more_inputs`. Each recipient keeps its own list's
        # segment; one in two lists is sent once, from the first.
        self.parts = [InputPart(self.input_path, self.segment, user_id_column), *more_inputs]
        self._uses_user_ids = any(part.user_id_column is not None for part in self.parts)
        self._user_id_conflicts = 0
        # Phones that must never get this campaign (opt-out list).
        self.opt_out = opt_out or frozenset()
        self.frequency_cap = frequency_cap
        # Daily hours SMS may go out; None = any time.
        self.send_window = send_window
        self.clock = clock
        self._window_closed = threading.Event()
        # Campaign identity: the DB is bound to this name and to `settings`
        # (what it sends, see `campaign_settings`) on the first run.
        self.campaign = campaign
        self.settings = settings
        self.allow_settings_change = allow_settings_change
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
        self._reporter: Reporter = reporter or TqdmReporter()
        self.install_signal_handlers = install_signal_handlers
        # phone → per-recipient tokens, filled from the input by `run()`.
        # None means every recipient gets the static tokens in SenderConfig.
        self._row_tokens: dict[str, dict[str, str]] | None = None
        self._bucket = TokenBucket(rate_per_sec)
        # `_stop`: claim nothing more (a halt or a cancel). `_cancelled`: the
        # operator asked for it, so the run reports itself as stopped.
        self._stop = threading.Event()
        self._cancelled = threading.Event()
        self._error_counter: Counter[str] = Counter()
        self._error_lock = threading.Lock()
        # Pre-send figures: credit from account/info, a real per-SMS cost from
        # the approval test, and what this run has spent so far.
        self._credit: int | None = None
        self._approval_cost: int | None = cost_per_sms
        self._approval_message_id: int | None = None
        self._estimate: int | None = None
        self._run_cost = 0
        self._stop_reason: tuple[str, dict] | None = None
        self._halt_code: int | None = None  # the first HaltError while sending

    def _add_cost(self, cost: int | None) -> None:
        if cost:
            with self._error_lock:
                self._run_cost += cost

    def cancel(self) -> None:
        """Stop gracefully, from any thread: no further recipient is claimed,
        requests already in flight finish and are recorded, and the rest stay
        claimable for the next run. Ctrl-C does the same in the CLI."""
        self._cancelled.set()
        self._stop.set()

    def _close_window(self) -> None:
        """The sending window closed mid-run: claim nothing more. The rest
        stay claimable, so the next run inside the window continues."""
        if not self._window_closed.is_set():
            self._window_closed.set()
            logger.warning("send_window_closed", extra={"window": str(self.send_window)})
        self._stop.set()

    def _record_error(self, status_code: int | None, message: str) -> None:
        """Bucket failures by `[code] message` for the end-of-run report."""
        safe = redact_secrets(message)
        key = f"[{status_code if status_code is not None else 'net'}] {safe}"
        with self._error_lock:
            self._error_counter[key] += 1

    @contextmanager
    def _signals(self) -> Iterator[None]:
        """Ctrl-C / SIGTERM → graceful stop; a second one forces it. The
        previous handlers come back afterwards, so an embedding process
        keeps its own."""
        if not self.install_signal_handlers:
            yield
            return

        def handler(signum, _frame):
            if self._cancelled.is_set():
                logger.warning("force_exit", extra={"signal": signum})
                raise KeyboardInterrupt
            logger.warning(
                "graceful_shutdown_requested",
                extra={"signal": signum, "hint": "press_again_to_force"},
            )
            self.cancel()

        previous = {}
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                previous[sig] = signal.signal(sig, handler)
            except ValueError:
                # Not on the main thread (e.g. a worker thread) — skip.
                pass
        try:
            yield
        finally:
            for sig, old in previous.items():
                signal.signal(sig, old)

    def _send_one(self, phone: str) -> tuple[str, str]:
        if self._stop.is_set():
            return ("skipped", phone)
        # Rate-limit BEFORE claim so we don't lock the row while sleeping.
        self._bucket.acquire()
        if self._stop.is_set():
            return ("skipped", phone)
        if self.send_window and not self.send_window.contains(self.clock()):
            self._close_window()
            return ("skipped", phone)
        # Before the claim: a recipient whose link is missing is skipped,
        # never claimed and never sent without it.
        tokens = self._tokens_for(phone)
        if self.links is not None and (tokens or {}).get(self.links.token) is None:
            logger.error("link_missing", extra={"phone": phone})
            return ("skipped", phone)
        recipient = self.state.claim(phone, link_key=self._link_keys.get(phone))
        if recipient is None:
            # Already sent or claimed by someone else.
            return ("already_done", phone)
        try:
            result = self.sender.send(phone, tokens=tokens)
        except HaltError as e:
            self.state.mark_failed(phone, e.status_code, e.message, permanent=False)
            self._record_error(e.status_code, e.message)
            if self._halt_code is None:
                self._halt_code = e.status_code
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
            self._add_cost(result.cost)
            logger.info(
                "send_ok",
                extra={
                    "phone": phone,
                    "msg_id": result.message_id,
                    "attempts": recipient.attempts,
                },
            )
            return ("sent", phone)

    def _tokens_for(self, phone: str) -> dict[str, str] | None:
        """Per-recipient tokens (CSV columns) plus the recipient's link;
        None when every recipient gets the static tokens only."""
        tokens = dict(self._row_tokens.get(phone) or {}) if self._row_tokens is not None else {}
        if self.links is not None and self._link_tokens is not None:
            link = self._link_tokens.get(phone)
            if link is not None:
                tokens[self.links.token] = link
        return tokens or None

    def _links_stage(self, phones: list[str]) -> None:
        """Stage 2: every short link this run's SMS need, before any SMS —
        the approval test's own link included. Raises PreflightError when
        one can't be made; the links already made are kept for next time."""
        if self.links is None or not (phones or self.approval_test_number):
            return
        assert self.link_client is not None and self.campaign
        segments = self.state.segments_for(phones)
        stage = LinkStage(
            self.state, self.link_client, campaign=self.campaign, settings=self.links,
            workers=self.link_workers, rate_per_sec=self.link_rate_per_sec,
            stop=self._stop, note=self._reporter.note,
            # A reporter may show link progress too (the dashboard's does).
            progress=getattr(self._reporter, "links", None),
        )
        # A test run makes only the test SMS's own link: the recipients' links
        # are made when sending starts, still all before the first SMS
        # (decided 2026-10-04), so the operator gets the test SMS in seconds.
        recipients = {} if self.test_only else {p: segments.get(p) or self.segment for p in phones}
        try:
            result = stage.run(recipients, test_phone=self.approval_test_number)
        except ShlinkHaltError as e:
            logger.error("links_halt", extra={"status": e.status, "detail": str(e)})
            raise PreflightError(
                f"Shlink refused the link stage: {e}", "links_refused", status=e.status,
            ) from e
        except LinkError as e:
            logger.error("links_not_ready", extra={"detail": str(e)})
            raise PreflightError(f"links: {e}", "links_failed") from e
        self._link_tokens = result.tokens
        self._link_keys = result.keys
        self._test_link_token = result.test_token
        self._links_ready, self._links_created = result.needed, result.created
        # (`created` would clash with LogRecord's own attribute.)
        logger.info(
            "links_ready",
            extra={"links_needed": result.needed, "links_created": result.created,
                   "links_extended": result.extended, "strategy": self.links.strategy},
        )
        extended = f", {result.extended} given more time" if result.extended else ""
        self._reporter.note(
            f"Links: {result.needed} ready ({result.created} created now{extended}).",
            "links_ready", needed=result.needed, created=result.created, extended=result.extended,
        )

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
            raise PreflightError(
                f"account check failed: [{e.status_code}] {e.message}", "account_refused",
                code=e.status_code,
            ) from e
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
            self._credit = info.remaining_credit
            self._reporter.note(
                f"Account: credit={info.remaining_credit} "
                f"expires={info.expire_date or '?'} type={info.type or '?'}",
                "account", credit=info.remaining_credit,
            )
            if phones and info.remaining_credit <= 0:
                raise PreflightError(
                    f"remaining credit is {info.remaining_credit}; top up before sending",
                    "no_credit", credit=info.remaining_credit,
                )
        self._check_account_config()

    def _check_account_config(self) -> None:
        """Account settings that change what a run does. Read-only and
        best-effort: if they can't be read, the run goes on."""
        if not hasattr(self.sender, "account_config"):
            return  # test fakes / alternate senders
        try:
            config = self.sender.account_config()
        except SendError as e:  # incl. HaltError, e.g. no access to this method
            logger.warning(
                "preflight_config_unreadable",
                extra={"status": e.status_code, "detail": e.message},
            )
            return
        logger.info(
            "preflight_config",
            extra={"debug_mode": config.debug_mode, "resend_failed": config.resend_failed},
        )
        if config.debug_mode:
            raise PreflightError(
                "the Kavenegar account is in debug mode, so nothing would be delivered "
                "(every SMS is cancelled); turn it off in the Kavenegar panel first",
                "debug_mode",
            )
        if config.resend_failed:
            self._reporter.note(
                "Note: Kavenegar's 'resend failed' setting is on, so it resends "
                "undelivered SMS once by itself.",
                "resend_failed_on",
            )

    def _check_credit(self, phones: list[str]) -> None:
        """Estimate the run's cost from a real SMS — the approval test's, else
        what this campaign already paid per SMS — and refuse a run the credit
        can't cover. Skipped while either number is unknown."""
        if not phones or self._credit is None:
            return
        per_sms = self._approval_cost or self.state.average_cost()
        if not per_sms:
            self._reporter.note(
                "Cost estimate: unknown until one SMS has gone out "
                "(--approval-test sends one first)."
            )
            return
        estimate = per_sms * len(phones)
        self._estimate = estimate
        varies = " (per-recipient tokens: the real cost varies with length)" \
            if self.token_columns is not None else ""
        self._reporter.note(
            f"Estimated cost: {len(phones)} SMS × {per_sms} = {estimate} rials; "
            f"credit {self._credit} rials{varies}.",
            "cost_estimate", count=len(phones), per_sms=per_sms, estimate=estimate, credit=self._credit,
        )
        logger.info(
            "cost_estimate",
            extra={"recipients": len(phones), "per_sms": per_sms, "estimate": estimate,
                   "credit": self._credit},
        )
        if estimate > self._credit:
            raise PreflightError(
                f"not enough credit: about {estimate} rials needed for {len(phones)} "
                f"SMS, {self._credit} left",
                "not_enough_credit", estimate=estimate, recipients=len(phones), credit=self._credit,
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
                raise PreflightError(
                    "approval test needs a recipient row to borrow tokens from", "test_needs_recipient",
                )
            tokens = self._row_tokens[sample]
            self._reporter.note(f"Approval test uses the tokens of {sample}.", "test_tokens_from", phone=sample)
        if self.links is not None and self._test_link_token is not None:
            # Its own link, so the operator's click never counts for a recipient.
            tokens = {**(tokens or {}), self.links.token: self._test_link_token}
        logger.info("approval_test_target", extra={"phone": target})
        self._reporter.note(f"Approval test: sending to {target} synchronously …", "test_sending", phone=target)

        kind = _ATTEMPT_KIND.set("test")
        try:
            result = self.sender.send(target, tokens=tokens)
        except HaltError as e:
            self._record_error(e.status_code, e.message)
            raise PreflightError(
                f"approval test send to {target} halted: [{e.status_code}] {e.message}",
                "test_refused", code=e.status_code,
            ) from e
        except SendError as e:
            # PermanentSendError or retries-exhausted SendError — both fatal here.
            self._record_error(e.status_code, e.message)
            raise PreflightError(
                f"approval test send to {target} failed: [{e.status_code}] {e.message}",
                "test_failed", code=e.status_code,
            ) from e
        finally:
            _ATTEMPT_KIND.reset(kind)

        logger.info(
            "approval_test_sent",
            extra={"phone": target, "msg_id": result.message_id},
        )
        self._approval_cost = result.cost or self._approval_cost
        self._approval_message_id = result.message_id
        self._add_cost(result.cost)
        if self.test_only:
            self._reporter.note("Test SMS sent; it's approved in the dashboard before sending.", "test_sent")
            return
        approved = self._approval_prompt(target, len(phones))
        if not approved:
            logger.warning("approval_declined", extra={"phone": target})
            raise PreflightError(
                "approval declined by operator; aborting before fan-out", "approval_declined",
            )
        logger.info("approval_granted", extra={"phone": target})
        self._reporter.note("Approval granted.")

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
            self._reporter.note(
                f"Checked {result.checked} unknown row(s) with Kavenegar: "
                f"{result.sent} had been sent, {result.requeued} had not (safe to "
                f"send again), {result.needs_review} need review.",
                "reconciled", checked=result.checked, sent=result.sent,
                requeued=result.requeued, needs_review=result.needs_review,
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
        self._reporter.note(f"Smoke test: sending to {target} synchronously …", "smoke_sending", phone=target)
        outcome, _ = self._send_one(target)
        if outcome != "sent":
            raise PreflightError(
                f"smoke test to {target} did not succeed (outcome={outcome}); "
                "fix the issue (template/tokens/account) before sending the rest",
                "smoke_failed", outcome=outcome,
            )
        self._reporter.note("Smoke test passed.", "smoke_passed")

    def run(self) -> RunSummary:
        # One process per DB: a second run would treat our `in_flight` rows
        # as orphans and send them again. Raises RunLockError if taken.
        with RunLock(self.state.db_path), self._signals():
            return self._run()

    def _run(self) -> RunSummary:
        loaded, new_count, phones = self._prepare()
        started = time.monotonic()
        try:
            phones, smoke_sent = self._preflight_checks(phones)
        except PreflightError as e:
            logger.error("preflight_failed", extra={"detail": str(e)})
            self._reporter.note(f"Preflight failed: {e}")
            if not self._cancelled.is_set():
                self._stop_reason = (e.key, e.fields)
            # A cancel during the link stage is a stop, not a halt.
            summary = self._build_summary(
                loaded=loaded, new_count=new_count, counts=SendCounts(),
                halted=not self._cancelled.is_set(), started=started,
            )
            if not self.test_only:
                self._record_last_run(summary)
            return summary

        if self.test_only:
            # Not the campaign's "last run": nothing was sent to recipients.
            summary = self._build_summary(
                loaded=loaded, new_count=new_count, counts=SendCounts(),
                halted=False, started=started,
            )
            logger.info("test_run_end", extra=_summary_log_fields(summary))
            return summary

        counts, halted = self._fan_out(phones)
        counts.sent += smoke_sent
        if self._window_closed.is_set():
            self._reporter.note(
                f"The sending window ({self.send_window}) closed. Re-run the same "
                "command inside it to continue.",
                "window_closed",
            )

        # On a long run, rows that went `unknown` early may be old enough now.
        if not halted and not self._stop.is_set():
            self._reconcile_unknown("end")

        summary = self._build_summary(
            loaded=loaded, new_count=new_count, counts=counts,
            halted=halted, started=started,
        )
        logger.info("run_end", extra=_summary_log_fields(summary))
        self._record_last_run(summary)
        return summary

    def _record_last_run(self, summary: RunSummary) -> None:
        """Keep the latest run's numbers with the campaign (`status`, dashboard)."""
        self.state.set_meta(
            "last_run", json.dumps({"at": time.time(), **_summary_log_fields(summary)}),
        )

    def _prepare(self) -> tuple[LoadResult, int, list[str]]:
        """Bind the campaign, load the input, seed the state DB, settle
        leftovers from earlier runs, and return (input, new rows, phones to
        send in order). Raises CampaignMismatchError before touching rows."""
        if self.campaign or self.settings is not None:
            changed = self.state.bind_campaign(
                self.campaign, self.settings, allow_change=self.allow_settings_change,
            )
            if changed:
                logger.warning("campaign_settings_changed", extra={"changed": ", ".join(changed)})
                self._reporter.note(f"Campaign settings changed: {', '.join(changed)}.",
                                    "settings_changed", changed=changed)
        loaded = input_loader.load_parts(self.parts, self.token_columns)
        if self.token_columns is not None:
            self._row_tokens = {r.phone: r.tokens for r in loaded.valid}
        logger.info(
            "input_loaded",
            extra={
                "path": ", ".join(str(part.path) for part in self.parts),
                "segment": ", ".join(part.segment for part in self.parts),
                "valid": len(loaded.valid),
                "invalid": len(loaded.invalid),
                "duplicates_collapsed": loaded.duplicates_collapsed,
                "header": loaded.header,
                "missing_user_id": loaded.missing_user_id,
                "user_id_conflicts": len(loaded.conflicts),
            },
        )

        if loaded.invalid:
            self.state.record_invalid_many(
                [(inv.raw, inv.reason) for inv in loaded.invalid]
            )
        by_segment: dict[str, list[tuple[str, str]]] = {}
        for r in loaded.valid:
            by_segment.setdefault(loaded.segments.get(r.phone, self.segment), []).append((r.phone, r.raw))
        new_count = sum(
            self.state.upsert_pending(rows, segment=name) for name, rows in by_segment.items()
        )
        conflicting = self._record_user_ids(loaded) if self._uses_user_ids else set()
        orphans = self.state.mark_orphans_unknown()
        if orphans:
            logger.warning("orphans_marked_unknown", extra={"n": orphans})
            self._reporter.note(
                f"{orphans} recipient(s) were mid-send when the last run stopped. They are "
                "marked unknown and NOT resent: they may already have the SMS.",
                "orphans", n=orphans,
            )
        # Settle old-enough `unknown` rows first, so the ones Kavenegar never
        # got go out in this run like everyone else.
        self._reconcile_unknown("start")
        # Exclusions go last, right before the queue is read: a row that
        # reconciliation just made claimable again must not slip past them.
        self._exclude_conflicts(conflicting)
        if self.opt_out:
            suppressed = self.state.suppress(self.opt_out)
            if suppressed:
                logger.info("opted_out_suppressed", extra={"n": suppressed})
                self._reporter.note(
                    f"{suppressed} recipient(s) are on the opt-out list and won't be sent.",
                    "suppressed", n=suppressed,
                )

        if self.frequency_cap is not None:
            self._apply_frequency_cap()

        phones = self.state.list_claimable_phones()
        if self._row_tokens is not None:
            # Per-recipient tokens live in the input file, so a claimable row
            # left in the DB by a different input has nothing to send — leave it.
            not_in_input = sum(1 for p in phones if p not in self._row_tokens)
            if not_in_input:
                logger.warning("skipped_not_in_input", extra={"n": not_in_input})
                self._reporter.note(
                    f"Skipping {not_in_input} claimable row(s) that aren't in "
                    f"{', '.join(part.path.name for part in self.parts)} "
                    "(no per-recipient tokens for them).",
                    "not_in_input", n=not_in_input,
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
        return loaded, new_count, phones

    def _record_user_ids(self, loaded: LoadResult) -> set[str]:
        """Record user IDs and return the phones that came with two
        different ones — within the input, or against an earlier import.
        They can't be attributed to anyone, so they aren't sent (decided
        2026-10-04); `_exclude_conflicts` takes them out of the queue."""
        self.state.set_meta("user_id_column", next(
            (part.user_id_column for part in self.parts if part.user_id_column is not None), "",
        ))
        earlier = self.state.assign_user_ids(
            {r.phone: r.user_id for r in loaded.valid if r.user_id}
        )
        for phone, stored, new in earlier:
            logger.warning(
                "user_id_conflict", extra={"phone": phone, "stored": stored, "new": new},
            )
        if loaded.missing_user_id:
            self._reporter.note(
                f"{loaded.missing_user_id} recipient(s) have no user ID: they're sent, "
                "and reported as missing user ID.",
                "missing_user_id", n=loaded.missing_user_id,
            )
        return set(loaded.conflicts) | {phone for phone, _, _ in earlier}

    def _apply_frequency_cap(self) -> None:
        """Hold back whoever got as many SMS as the cap allows within its
        days, across the folder's campaigns. Counted afresh each run: last
        run's capped rows go back first, as the window has moved on."""
        cap = self.frequency_cap
        self.state.uncap()
        counts = folder_sends_since(Path(self.state.db_path).parent, time.time() - cap.seconds)
        over = [p for p in self.state.list_claimable_phones() if counts.get(p, 0) >= cap.sms]
        capped = self.state.cap(over)
        if capped:
            logger.info("frequency_capped", extra={"n": capped, "cap": str(cap)})
            self._reporter.note(
                f"{capped} recipient(s) already got {cap.sms} SMS in the last {cap.days} days "
                "(the frequency cap) and won't be sent this time.",
                "capped", n=capped, sms=cap.sms, days=cap.days,
            )

    def _exclude_conflicts(self, conflicting: set[str]) -> None:
        """`invalid` is never claimable, and unlike `failed_permanent` no
        retry resets it — only an explicit `reset --status invalid`. Rows
        already sent stay `sent`."""
        if not conflicting:
            return
        excluded = self.state.exclude(conflicting, "conflicting user IDs; not sent")
        self._user_id_conflicts = len(conflicting)
        self._reporter.note(
            f"{len(conflicting)} phone(s) came with two different user IDs and "
            f"won't be sent ({excluded} taken out of the queue; any already "
            "sent stay sent). Fix the IDs at the source.",
            "user_id_conflicts", n=len(conflicting),
        )

    def _preflight_checks(self, phones: list[str]) -> tuple[list[str], int]:
        """Account check → optional approval test (manual gate) → optional
        smoke test (auto). Raises PreflightError to abort before fan-out.
        Returns the phones still to send and how many the smoke test sent."""
        self._check_allowlist(phones)
        if self.send_window and phones:
            now = self.clock()
            if not self.send_window.contains(now):
                raise PreflightError(
                    f"outside the sending window ({self.send_window}): it's "
                    f"{now.astimezone(TEHRAN):%H:%M} there; run again inside it",
                    "outside_window", start=f"{self.send_window.start:%H:%M}",
                    end=f"{self.send_window.end:%H:%M}", now=f"{now.astimezone(TEHRAN):%H:%M}",
                )
        self._preflight(phones)
        self._links_stage(phones)
        self._approval_test(phones)
        self._check_credit(phones)
        if self.test_only:
            return phones, 0
        self._smoke_test_run(phones)
        if self.smoke_test and self.preflight and phones:
            # The smoke test consumed phones[0] synchronously.
            return phones[1:], 1
        return phones, 0

    def _check_allowlist(self, phones: list[str]) -> None:
        """Restricted sending (allowlist.py): refuse before anything is sent,
        and before any link is made, when the test number or anyone in the
        queue isn't an allowed number. The sender refuses them too; this
        says so up front, with the count."""
        allowed = getattr(self.sender, "allowlist", None)
        if allowed is None:
            return
        if allowed.invalid:
            raise PreflightError(
                f"{ENV_ALLOWED_NUMBERS} holds {allowed.invalid} value(s) that aren't phone numbers; "
                "nothing is sent until it's fixed",
                "allowlist_invalid", count=allowed.invalid,
            )
        if self.approval_test_number and not allowed.allows(self.approval_test_number):
            raise PreflightError(
                f"restricted sending: the test number isn't on {ENV_ALLOWED_NUMBERS}",
                "test_number_not_allowed",
            )
        if self.test_only:
            return
        outside = sum(1 for phone in phones if not allowed.allows(phone))
        if outside:
            raise PreflightError(
                f"restricted sending: {outside} recipient(s) aren't on {ENV_ALLOWED_NUMBERS}; nothing was sent",
                "recipients_not_allowed", count=outside,
            )

    def _fan_out(self, phones: list[str]) -> tuple[SendCounts, bool]:
        """Send to every phone across the worker pool. Returns the tallies
        and whether a HaltError stopped the run."""
        counts = SendCounts(total=len(phones))
        halted = False
        if not phones:
            return counts, halted
        self._reporter.start(len(phones))
        try:
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
                            counts.sent += 1
                        elif outcome == "failed_permanent":
                            counts.failed_permanent += 1
                        elif outcome == "failed_retriable":
                            counts.failed_retriable += 1
                        elif outcome == "unknown":
                            counts.unknown += 1
                        elif outcome in ("already_done", "skipped"):
                            counts.already_done += 1
                        self._reporter.advance(counts)
                finally:
                    if halted or self._stop.is_set():
                        for f in futures:
                            f.cancel()
        finally:
            self._reporter.finish()
        return counts, halted

    def _build_summary(
        self, *, loaded: LoadResult, new_count: int, counts: SendCounts,
        halted: bool, started: float,
    ) -> RunSummary:
        elapsed = max(0.0, time.monotonic() - started)
        rate = (counts.sent / elapsed) if elapsed > 0 else 0.0
        with self._error_lock:
            top = tuple(self._error_counter.most_common(3))
        db_counts = self.state.counts()
        reason, fields = self._stop_reason or (None, {})
        if reason is None and halted and self._halt_code is not None:
            reason, fields = "provider_halt", {"code": self._halt_code}
        elif reason is None and self._window_closed.is_set() and self.send_window is not None:
            reason, fields = "window_closed", {
                "start": f"{self.send_window.start:%H:%M}", "end": f"{self.send_window.end:%H:%M}",
            }
        return RunSummary(
            total_input=len(loaded.valid) + len(loaded.invalid),
            new_recipients=new_count,
            duplicates_collapsed=loaded.duplicates_collapsed,
            invalid=len(loaded.invalid),
            sent=counts.sent,
            failed_permanent=counts.failed_permanent,
            failed_retriable=counts.failed_retriable,
            halted=halted,
            elapsed_sec=round(elapsed, 3),
            sends_per_sec=round(rate, 3),
            top_errors=top,
            already_done=counts.already_done,
            unknown=db_counts.get(UNKNOWN, 0),
            needs_review=db_counts.get(NEEDS_REVIEW, 0),
            stopped=self._cancelled.is_set() or self._window_closed.is_set(),
            suppressed=db_counts.get(SUPPRESSED, 0),
            capped=db_counts.get(CAPPED, 0),
            cost=self._run_cost,
            missing_user_id=loaded.missing_user_id,
            user_id_conflicts=self._user_id_conflicts,
            links_ready=self._links_ready,
            links_created=self._links_created,
            credit=self._credit,
            cost_per_sms=self._approval_cost or self.state.average_cost() or None,
            estimate=self._estimate,
            test_message_id=self._approval_message_id,
            test_only=self.test_only,
            stop_reason=reason,
            stop_fields=fields,
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
        "suppressed": s.suppressed,
        "capped": s.capped,
        "cost": s.cost,
        "missing_user_id": s.missing_user_id,
        "user_id_conflicts": s.user_id_conflicts,
        "links_ready": s.links_ready,
        "links_created": s.links_created,
        "halted": s.halted,
        "stopped": s.stopped,
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
    if s.suppressed:
        lines.append(f"  suppressed        {s.suppressed}  (on the opt-out list; never sent)")
    if s.capped:
        lines.append(f"  capped            {s.capped}  (over the frequency cap; counted again next run)")
    if s.missing_user_id:
        lines.append(f"  missing user ID   {s.missing_user_id}  (sent; not attributable to a user)")
    if s.user_id_conflicts:
        lines.append(f"  user ID conflict  {s.user_id_conflicts}  (two different user IDs; not sent)")
    if s.links_ready:
        lines.append(f"  links             {s.links_ready}  ({s.links_created} created this run)")
    if s.stopped:
        lines.append("  stopped           True  (re-run the same command to continue)")
    if s.cost:
        lines.append(f"  cost              {s.cost:,} rials")
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


# What `_attempt_recorder` files a call under: "send" for a recipient, or
# "test" while the approval test runs. A context variable, because the
# Sender reports the call from inside `send()`; the fan-out's threads start
# with the default.
_ATTEMPT_KIND: ContextVar[str] = ContextVar("attempt_kind", default="send")


def _attempt_recorder(state: StateStore) -> Callable[[Attempt], None]:
    """Write each call to Kavenegar into the state DB's `attempts` table."""
    def record(a: Attempt) -> None:
        state.record_attempt(
            phone=a.phone, kind=_ATTEMPT_KIND.get(), outcome=a.outcome,
            started_at=a.started_at, finished_at=a.finished_at,
            status_code=a.status_code, message_id=a.message_id, cost=a.cost,
            detail=a.detail,
        )
    return record


def campaign_settings(
    cfg: SenderConfig, token_columns: TokenColumns | None, links: LinkSettings | None = None,
) -> dict:
    """What a campaign sends — the part that must stay the same across its
    runs. Throughput knobs (workers, rate, timeouts) and the input file are
    not part of it: one campaign can be fed several inputs (segments)."""
    settings = {
        "template": cfg.template,
        "tokens": {
            name: getattr(cfg, name)
            for name in TOKEN_MAX_SPACES if getattr(cfg, name) is not None
        },
        "token_columns": dict(token_columns.columns) if token_columns else {},
        "value_maps": (
            {col: dict(m) for col, m in token_columns.value_maps.items()}
            if token_columns else {}
        ),
    }
    if links is not None:
        # Absent without links, so campaigns from before links keep matching.
        settings["links"] = links.as_settings()
    return settings


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
    campaign: str | None = None,
    allow_settings_change: bool = False,
    opt_out: frozenset[str] | None = None,
    frequency_cap: FrequencyCap | None = None,
    send_window: SendWindow | None = None,
    user_id_column: str | None = None,
    segment: str | None = None,
    more_inputs: Sequence[InputPart] = (),
    links: LinkSettings | None = None,
    link_client: LinkClient | None = None,
    link_rate_per_sec: float = 10.0,
    reporter: Reporter | None = None,
    install_signal_handlers: bool = True,
    test_only: bool = False,
    cost_per_sms: int | None = None,
    make_sender: Callable[..., Sender] = Sender,
) -> Runner:
    """A runner for these settings. `make_sender(cfg, on_attempt=…)` builds
    the Kavenegar client; the dashboard's sandbox passes a simulated one."""
    state = StateStore(db_path)
    sender = make_sender(sender_cfg, on_attempt=_attempt_recorder(state))
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
        campaign=campaign,
        settings=campaign_settings(sender_cfg, token_columns, links),
        allow_settings_change=allow_settings_change,
        opt_out=opt_out,
        frequency_cap=frequency_cap,
        send_window=send_window,
        user_id_column=user_id_column,
        segment=segment,
        more_inputs=more_inputs,
        links=links,
        link_client=link_client,
        link_rate_per_sec=link_rate_per_sec,
        reporter=reporter,
        install_signal_handlers=install_signal_handlers,
        test_only=test_only,
        cost_per_sms=cost_per_sms,
    )
