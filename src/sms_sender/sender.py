"""Kavenegar verify/lookup wrapper with retry policy and error classification."""
from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass
from typing import Callable, Protocol

import requests
import urllib3
from kavenegar import APIException, HTTPException
from tenacity import (
    RetryCallState,
    RetryError,
    Retrying,
    retry_if_exception_type,
    stop_after_attempt,
    wait_random_exponential,
)

from .allowlist import ENV_ALLOWED_NUMBERS, Allowlist, allowlist
from .classifier import Action, classify
from .redact import redact_secrets

logger = logging.getLogger(__name__)

# Two formats seen in the wild:
#   real SDK / our HTTP wrapper: "APIException[418] insufficient credit"
#   bracketed (legacy):          "APIException[418 insufficient credit]"
_API_EXC_RE_OUTSIDE = re.compile(r"\[(\d+)\]\s*(.*)")
_API_EXC_RE_INSIDE = re.compile(r"\[(\d+)\s+(.+?)\]")

# Kavenegar's token-format rules: token/2/3 reject spaces entirely; token10
# allows up to 5; token20 up to 8. Key order is the order params are sent in.
TOKEN_MAX_SPACES: dict[str, int] = {
    "token": 0, "token2": 0, "token3": 0, "token10": 5, "token20": 8,
}
TOKEN_MAX_LEN = 100


def token_issue(name: str, value: str) -> tuple[str, dict] | None:
    """Why Kavenegar would reject `value` as token `name` (error 431), as a
    key and its numbers, or None if it's fine: at most 100 characters, no
    line break or underscore, and no more spaces than the token allows."""
    if len(value) > TOKEN_MAX_LEN:
        return "too_long", {"length": len(value), "max": TOKEN_MAX_LEN}
    if "\n" in value or "\r" in value or "\t" in value:
        return "line_break", {}
    if "_" in value:
        return "underscore", {}
    spaces = value.count(" ")
    if spaces > TOKEN_MAX_SPACES[name]:
        return "too_many_spaces", {"max": TOKEN_MAX_SPACES[name], "spaces": spaces}
    return None


def token_problem(name: str, value: str) -> str | None:
    """`token_issue` in words, for the CLI and the logs."""
    issue = token_issue(name, value)
    if issue is None:
        return None
    key, f = issue
    return {
        "too_long": f"{name} is {f.get('length')} characters; Kavenegar allows at most {f.get('max')}",
        "line_break": f"{name} contains a line break or tab, which Kavenegar rejects",
        "underscore": f"{name} contains '_', which Kavenegar rejects",
        "too_many_spaces": f"{name} allows at most {f.get('max')} space(s); got {f.get('spaces')}",
    }[key]


# Lookup methods (e.g. sms/statusbyreceptor) answer an empty result with this
# code — "رکوردی با مشخصات مورد نظر پیدا نشد" (no record found) — not with [].
_NO_RECORD = 449


class SendError(Exception):
    """Base for non-retriable send errors. Carries the Kavenegar status code."""

    def __init__(self, status_code: int | None, message: str):
        super().__init__(message)
        self.status_code = status_code
        self.message = message


class PermanentSendError(SendError):
    pass


class HaltError(SendError):
    """Account/auth/quota error — abort the whole run."""


class UncertainSendError(SendError):
    """The request may have reached Kavenegar, but no clear answer came back
    (read timeout, dropped connection, garbled reply). Never retried: a retry
    could deliver a second SMS. The row becomes `unknown` instead."""


class _RetriableSendError(Exception):
    """Internal: signal tenacity to retry. Not exposed."""

    def __init__(self, status_code: int | None, message: str):
        super().__init__(message)
        self.status_code = status_code
        self.message = message


class _NotSent(HTTPException):
    """The connection itself failed, so the request never left. Safe to retry."""


class _OutcomeUnknown(HTTPException):
    """The request may have been processed. Must not be retried blindly."""


@dataclass(frozen=True)
class SendResult:
    message_id: int | None
    status_code: int
    cost: int | None = None  # rials, as reported by Kavenegar


@dataclass(frozen=True)
class SenderConfig:
    api_key: str
    template: str
    token: str | None = None
    token2: str | None = None
    token3: str | None = None
    token10: str | None = None  # allows up to 5 spaces
    token20: str | None = None  # allows up to 8 spaces
    timeout: float = 15.0
    max_attempts: int = 5
    backoff_max: float = 30.0


@dataclass(frozen=True)
class AccountInfo:
    remaining_credit: int | None
    expire_date: str | None
    type: str | None


@dataclass(frozen=True)
class Attempt:
    """One call to Kavenegar, reported to `Sender(on_attempt=…)`.

    `outcome` is `accepted`, `retry` (refused before sending, retried),
    `rejected` (permanent), `halt`, or `unknown` (may have been sent).
    """
    phone: str
    outcome: str
    started_at: float
    finished_at: float
    status_code: int | None = None
    message_id: int | None = None
    cost: int | None = None
    detail: str | None = None


@dataclass(frozen=True)
class ProviderMessage:
    """A message Kavenegar reports for a phone (`sms/statusbyreceptor`)."""
    message_id: int
    status: int | None  # Kavenegar delivery status, e.g. 10 = delivered


@dataclass(frozen=True)
class AccountConfig:
    """Kavenegar account settings that change what a campaign does."""
    debug_mode: bool | None     # on: nothing is delivered, every SMS is cancelled
    resend_failed: bool | None  # on: Kavenegar resends undelivered SMS once itself


class _SDK(Protocol):
    def verify_lookup(self, params: dict) -> list[dict]: ...
    def account_info(self) -> dict: ...
    def account_config(self) -> dict: ...
    def status_by_receptor(self, receptor: str, startdate: int, enddate: int) -> list[dict]: ...
    def message_status(self, message_ids: list[int]) -> list[dict]: ...


def _parse_api_exception(exc: APIException) -> tuple[int | None, str]:
    s = str(exc)
    for pat in (_API_EXC_RE_OUTSIDE, _API_EXC_RE_INSIDE):
        m = pat.search(s)
        if m:
            try:
                return int(m.group(1)), m.group(2).strip().rstrip("'\"")
            except ValueError:
                continue
    return None, s


def _never_sent(exc: requests.exceptions.RequestException) -> bool:
    """True only when the request provably never left: the TCP connection
    itself failed (DNS failure, connection refused, connect timeout), possibly
    through the proxy. Anything later — a read timeout, a dropped connection,
    an SSL error mid-stream — may have reached Kavenegar."""
    if isinstance(exc, requests.exceptions.ConnectTimeout):
        return True
    if not isinstance(exc, requests.exceptions.ConnectionError) or not exc.args:
        return False
    reason = getattr(exc.args[0], "reason", None)  # urllib3 MaxRetryError
    if isinstance(reason, urllib3.exceptions.ProxyError):
        reason = getattr(reason, "original_error", None)
    return isinstance(reason, urllib3.exceptions.NewConnectionError)


def _may_have_been_processed(http_status: int) -> bool:
    """A garbled 200 came from the API itself; a 5xx may come from a gateway
    that already forwarded the request. Either way it may have been sent."""
    return http_status == 200 or http_status >= 500


class _KavenegarHTTP:
    """Thin replacement for the SDK's KavenegarAPI that respects a timeout.

    The packaged kavenegar SDK calls `requests.post(...)` with no timeout, so
    a hung connection would hang us forever. We do the POST ourselves and
    re-raise the SDK's exception types so the rest of the code is unchanged.
    Also normalizes the APIException message to `[CODE] message` format.
    """

    BASE = "https://api.kavenegar.com/v1/{key}/{path}.json"

    def __init__(self, api_key: str, timeout: float):
        self._verify_url = self.BASE.format(key=api_key, path="verify/lookup")
        self._account_url = self.BASE.format(key=api_key, path="account/info")
        self._config_url = self.BASE.format(key=api_key, path="account/config")
        self._status_by_receptor_url = self.BASE.format(key=api_key, path="sms/statusbyreceptor")
        self._status_url = self.BASE.format(key=api_key, path="sms/status")
        self._timeout = timeout
        self._session = requests.Session()

    def _post(self, url: str, params: dict | None = None) -> dict:
        try:
            resp = self._session.post(url, data=params or {}, timeout=self._timeout)
        except requests.exceptions.RequestException as e:
            raise _network_error(e) from e
        return self._parse(resp)

    def _get(self, url: str) -> dict:
        try:
            resp = self._session.get(url, timeout=self._timeout)
        except requests.exceptions.RequestException as e:
            raise _network_error(e) from e
        return self._parse(resp)

    def _parse(self, resp: requests.Response) -> dict:
        try:
            body = resp.json()
        except ValueError as e:
            # Non-JSON body (HTML 5xx page, gateway error, garbled response).
            detail = redact_secrets(f"non-json response (http {resp.status_code}): {e}")
            if _may_have_been_processed(resp.status_code):
                raise _OutcomeUnknown(detail) from e
            # A 3xx/4xx page means we never reached the API (bad URL, proxy
            # refusal) — permanent, retrying won't fix the setup.
            raise PermanentSendError(resp.status_code, detail) from e
        if not isinstance(body, dict) or "return" not in body:
            detail = f"malformed response (no `return` key): {str(body)[:200]}"
            if _may_have_been_processed(resp.status_code):
                raise _OutcomeUnknown(detail)
            raise PermanentSendError(None, detail)
        ret = body.get("return") or {}
        status = ret.get("status")
        if not isinstance(status, int):
            detail = f"malformed response (status not int): status={status!r}"
            if _may_have_been_processed(resp.status_code):
                raise _OutcomeUnknown(detail)
            raise PermanentSendError(None, detail)
        if status != 200:
            raise APIException(f"APIException[{status}] {ret.get('message', '')}")
        return body

    def verify_lookup(self, params: dict) -> list[dict]:
        body = self._post(self._verify_url, params)
        return body.get("entries") or []

    def account_info(self) -> dict:
        body = self._post(self._account_url)
        entries = body.get("entries") or {}
        if isinstance(entries, list):
            entries = entries[0] if entries else {}
        return entries

    def status_by_receptor(self, receptor: str, startdate: int, enddate: int) -> list[dict]:
        body = self._post(
            self._status_by_receptor_url,
            {"receptor": receptor, "startdate": startdate, "enddate": enddate},
        )
        return body.get("entries") or []

    def message_status(self, message_ids: list[int]) -> list[dict]:
        body = self._post(self._status_url, {"messageid": ",".join(map(str, message_ids))})
        return body.get("entries") or []

    def account_config(self) -> dict:
        # GET with no parameters only *reads* the settings; any parameter
        # would change that setting on the account. Never pass one here.
        body = self._get(self._config_url)
        entries = body.get("entries") or {}
        if isinstance(entries, list):
            entries = entries[0] if entries else {}
        return entries


def _network_error(exc: requests.exceptions.RequestException) -> HTTPException:
    """A request failure, as "it never left" (safe to retry) or "it may
    have been processed" (never retried blindly)."""
    if _never_sent(exc):
        return _NotSent(redact_secrets(str(exc)))
    return _OutcomeUnknown(redact_secrets(str(exc)))


_FROM_ENV = object()


class Sender:
    """One Sender per process — the SDK's KavenegarAPI is thread-safe (it's
    a thin wrapper over `requests`, and each call opens its own connection)."""

    def __init__(
        self, cfg: SenderConfig, sdk: _SDK | None = None, *,
        on_attempt: Callable[[Attempt], None] | None = None,
        allowed: Allowlist | None | object = _FROM_ENV,
    ):
        self.cfg = cfg
        self._sdk: _SDK = sdk or _KavenegarHTTP(cfg.api_key, cfg.timeout)
        # Audit hook, called once per call to Kavenegar (make_runner records
        # these in the state DB's `attempts` table).
        self._on_attempt = on_attempt
        # Restricted sending (allowlist.py): the only numbers an SMS may go to.
        self.allowlist: Allowlist | None = allowlist() if allowed is _FROM_ENV else allowed

    def build_params(self, phone: str, tokens: dict[str, str] | None = None) -> dict:
        """Return the exact POST body that would be sent for `phone`. No I/O.

        `tokens` carries per-recipient values (from CSV columns); they take
        precedence over the static tokens in `SenderConfig`.
        """
        params: dict = {"receptor": phone, "template": self.cfg.template}
        for name in TOKEN_MAX_SPACES:
            value = tokens[name] if tokens and name in tokens else getattr(self.cfg, name)
            if value is not None:
                params[name] = value
        return params

    def _do_call(self, phone: str, tokens: dict[str, str] | None) -> SendResult:
        try:
            response = self._sdk.verify_lookup(self.build_params(phone, tokens))
        except _NotSent as e:
            # The connection failed before the request left — safe to retry.
            raise _RetriableSendError(None, f"http: {e}") from e
        except HTTPException as e:
            # Any other network failure (read timeout, dropped connection,
            # garbled reply) may have been processed: never retry it blindly.
            raise UncertainSendError(None, f"outcome unknown: {e}") from e
        except APIException as e:
            code, message = _parse_api_exception(e)
            action = classify(code)
            if action is Action.HALT:
                raise HaltError(code, message) from e
            if action is Action.PERMANENT:
                raise PermanentSendError(code, message) from e
            # RETRY (or SUCCESS, which shouldn't raise APIException)
            raise _RetriableSendError(code, message) from e

        # Success path — Kavenegar returns a list of entries.
        if not response:
            # Status 200 means Kavenegar accepted the call, so the SMS may
            # well have gone out even without an entry to prove it.
            raise UncertainSendError(None, "status 200 but no entries from kavenegar")
        first = response[0]
        return SendResult(
            message_id=first.get("messageid"),
            status_code=int(first.get("status", 200)),
            cost=_safe_int(first.get("cost")),
        )

    def _call_and_report(self, phone: str, tokens: dict[str, str] | None) -> SendResult:
        """One call to Kavenegar, reported to `on_attempt` whatever happens."""
        started = time.time()
        try:
            result = self._do_call(phone, tokens)
        except Exception as e:
            self._report(Attempt(
                phone=phone, outcome=_attempt_outcome(e),
                started_at=started, finished_at=time.time(),
                status_code=getattr(e, "status_code", None),
                detail=redact_secrets(getattr(e, "message", None) or repr(e)),
            ))
            raise
        self._report(Attempt(
            phone=phone, outcome="accepted",
            started_at=started, finished_at=time.time(),
            status_code=result.status_code, message_id=result.message_id, cost=result.cost,
        ))
        return result

    def _report(self, attempt: Attempt) -> None:
        if self._on_attempt is None:
            return
        try:
            self._on_attempt(attempt)
        except Exception:  # noqa: BLE001 — an audit row must never change a send's outcome
            logger.exception("attempt_record_failed", extra={"phone": attempt.phone})

    def send(self, phone: str, tokens: dict[str, str] | None = None) -> SendResult:
        """Send one SMS, with retries on transient failures.

        `tokens` are per-recipient values layered over the static config.
        Raises HaltError for account/auth/quota issues (caller aborts the run).
        Raises PermanentSendError for per-recipient issues (caller marks failed).
        Raises UncertainSendError when the request may have been processed
        without a clear answer — never retried (caller marks it `unknown`).
        Raises SendError once retries of never-sent calls are exhausted.
        """
        if self.allowlist is not None and not self.allowlist.allows(phone):
            # Refused before any call, so nothing went out (the row stays
            # claimable: failed_retriable).
            raise HaltError(None, f"restricted sending: this number isn't on {ENV_ALLOWED_NUMBERS}")
        retryer = Retrying(
            stop=stop_after_attempt(self.cfg.max_attempts),
            wait=wait_random_exponential(multiplier=1, max=self.cfg.backoff_max),
            retry=retry_if_exception_type(_RetriableSendError),
            before_sleep=lambda state: _log_retry(phone, state),
            reraise=False,
        )
        try:
            for attempt in retryer:
                with attempt:
                    return self._call_and_report(phone, tokens)
        except RetryError as e:
            inner = e.last_attempt.exception()
            if isinstance(inner, _RetriableSendError):
                raise SendError(
                    inner.status_code, f"retries exhausted: {inner.message}"
                ) from e
            # Should not happen — tenacity only retries _RetriableSendError —
            # but if it ever does, surface the original cause faithfully.
            raise SendError(None, f"retries exhausted: {inner!r}") from e
        # `Retrying` always either `return`s from inside the loop or raises
        # `RetryError`, so this line is unreachable in practice. mypy/pyright
        # need a final return path, hence the explicit raise.
        raise SendError(None, "tenacity loop exited without result")

    def account_info(self) -> AccountInfo:
        """Fetch account info (credit, expiry, plan). Raises HaltError on auth issues."""
        try:
            data = self._sdk.account_info()
        except HTTPException as e:
            raise SendError(None, f"http: {e}") from e
        except APIException as e:
            code, message = _parse_api_exception(e)
            action = classify(code)
            if action is Action.HALT:
                raise HaltError(code, message) from e
            raise SendError(code, message) from e
        return AccountInfo(
            remaining_credit=_safe_int(data.get("remaincredit")),
            expire_date=data.get("expiredate"),
            type=data.get("type"),
        )

    def account_config(self) -> AccountConfig:
        """Read (never change) the account settings that matter to a run.
        Raises HaltError on account problems, SendError when unreachable."""
        try:
            data = self._sdk.account_config()
        except HTTPException as e:
            raise SendError(None, f"http: {e}") from e
        except APIException as e:
            code, message = _parse_api_exception(e)
            if classify(code) is Action.HALT:
                raise HaltError(code, message) from e
            raise SendError(code, message) from e
        return AccountConfig(
            debug_mode=_flag(data.get("debugmode")),
            resend_failed=_flag(data.get("resendfailed")),
        )

    def delivery_statuses(self, message_ids: list[int]) -> dict[int, int]:
        """Kavenegar's delivery status per message ID (`sms/status`: at most
        500 IDs per call, and only within 48 h of sending). Read-only.
        Raises HaltError on account problems, SendError when unreachable."""
        try:
            entries = self._sdk.message_status(message_ids)
        except HTTPException as e:
            raise SendError(None, f"http: {e}") from e
        except APIException as e:
            code, message = _parse_api_exception(e)
            if code == _NO_RECORD:
                return {}
            if classify(code) is Action.HALT:
                raise HaltError(code, message) from e
            raise SendError(code, message) from e
        statuses = {}
        for entry in entries:
            message_id, status = _safe_int(entry.get("messageid")), _safe_int(entry.get("status"))
            if message_id is not None and status is not None:
                statuses[message_id] = status
        return statuses

    def find_messages(self, phone: str, start: float, end: float) -> list[ProviderMessage]:
        """Messages Kavenegar sent to `phone` between `start` and `end` (unix
        seconds; Kavenegar allows at most one day). Read-only — this is how
        `unknown` rows are settled. Raises HaltError on account problems and
        SendError when Kavenegar can't be asked right now."""
        try:
            entries = self._sdk.status_by_receptor(phone, int(start), int(end))
        except HTTPException as e:
            raise SendError(None, f"http: {e}") from e
        except APIException as e:
            code, message = _parse_api_exception(e)
            if code == _NO_RECORD:
                return []  # Kavenegar reports "nothing found" as an error code
            if classify(code) is Action.HALT:
                raise HaltError(code, message) from e
            raise SendError(code, message) from e
        messages = []
        for entry in entries:
            message_id = _safe_int(entry.get("messageid"))
            if message_id is not None:
                messages.append(ProviderMessage(message_id, _safe_int(entry.get("status"))))
        return messages


def _attempt_outcome(exc: BaseException) -> str:
    if isinstance(exc, HaltError):
        return "halt"
    if isinstance(exc, UncertainSendError):
        return "unknown"
    if isinstance(exc, PermanentSendError):
        return "rejected"
    if isinstance(exc, _RetriableSendError):
        return "retry"
    return "error"


def _log_retry(phone: str, state: RetryCallState) -> None:
    """Leave a per-phone trace of every retry. Only calls that provably
    never left (the connection failed: status=None) or that Kavenegar
    refused with "try later" (409/451) are retried, so a retry can't
    double-send; uncertain failures become `unknown` instead."""
    exc = state.outcome.exception() if state.outcome else None
    logger.warning(
        "send_retry",
        extra={
            "phone": phone,
            "attempt": state.attempt_number,
            "status": getattr(exc, "status_code", None),
            "detail": getattr(exc, "message", repr(exc)),
        },
    )


def _flag(value: object) -> bool | None:
    """Kavenegar setting values ("enabled" / "disabled", …) → bool; None if unrecognized."""
    text = str(value).strip().lower() if value is not None else ""
    if text in ("enabled", "enable", "true", "1", "on", "yes"):
        return True
    if text in ("disabled", "disable", "false", "0", "off", "no"):
        return False
    return None


def _safe_int(value: object) -> int | None:
    if value is None:
        return None
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
