"""Kavenegar verify/lookup wrapper with retry policy and error classification."""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Protocol

import requests
from kavenegar import APIException, HTTPException
from tenacity import (
    RetryCallState,
    RetryError,
    Retrying,
    retry_if_exception_type,
    stop_after_attempt,
    wait_random_exponential,
)

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


class _RetriableSendError(Exception):
    """Internal: signal tenacity to retry. Not exposed."""

    def __init__(self, status_code: int | None, message: str):
        super().__init__(message)
        self.status_code = status_code
        self.message = message


@dataclass(frozen=True)
class SendResult:
    message_id: int | None
    status_code: int


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


class _SDK(Protocol):
    def verify_lookup(self, params: dict) -> list[dict]: ...
    def account_info(self) -> dict: ...


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
        self._timeout = timeout
        self._session = requests.Session()

    def _post(self, url: str, params: dict | None = None) -> dict:
        try:
            resp = self._session.post(url, data=params or {}, timeout=self._timeout)
        except requests.exceptions.RequestException as e:
            raise HTTPException(redact_secrets(str(e))) from e
        try:
            body = resp.json()
        except ValueError as e:
            # Non-JSON body (HTML 5xx page, gateway error, garbled response).
            # Permanent — retrying won't fix a misconfigured upstream that
            # serves HTML where JSON is expected.
            raise PermanentSendError(
                resp.status_code,
                redact_secrets(f"non-json response (http {resp.status_code}): {e}"),
            ) from e
        if not isinstance(body, dict) or "return" not in body:
            raise PermanentSendError(
                None,
                f"malformed response (no `return` key): {str(body)[:200]}",
            )
        ret = body.get("return") or {}
        status = ret.get("status")
        if not isinstance(status, int):
            raise PermanentSendError(
                None,
                f"malformed response (status not int): status={status!r}",
            )
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


class Sender:
    """One Sender per process — the SDK's KavenegarAPI is thread-safe (it's
    a thin wrapper over `requests`, and each call opens its own connection)."""

    def __init__(self, cfg: SenderConfig, sdk: _SDK | None = None):
        self.cfg = cfg
        self._sdk: _SDK = sdk or _KavenegarHTTP(cfg.api_key, cfg.timeout)

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
        except HTTPException as e:
            # Network / timeout — always retriable.
            raise _RetriableSendError(None, f"http: {e}") from e
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
            raise PermanentSendError(None, "empty response from kavenegar")
        first = response[0]
        return SendResult(
            message_id=first.get("messageid"),
            status_code=int(first.get("status", 200)),
        )

    def send(self, phone: str, tokens: dict[str, str] | None = None) -> SendResult:
        """Send one SMS, with retries on transient failures.

        `tokens` are per-recipient values layered over the static config.
        Raises HaltError for account/auth/quota issues (caller aborts the run).
        Raises PermanentSendError for per-recipient issues (caller marks failed).
        Raises _RetriableSendError-wrapped RetryError after retries are exhausted.
        """
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
                    return self._do_call(phone, tokens)
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


def _log_retry(phone: str, state: RetryCallState) -> None:
    """Leave a per-phone trace of every retry. A network-level failure
    (status=None, e.g. a read timeout) may still have been delivered, so these
    lines are how to find recipients that might have received the SMS twice.
    A retried Kavenegar code (409/414/419) was rejected and never sent."""
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


def _safe_int(value: object) -> int | None:
    if value is None:
        return None
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
