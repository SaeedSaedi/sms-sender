# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```bash
# Install (editable, with test deps)
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"

# Run the CLI (entry point is `sms-sender = sms_sender.cli:cli`)
sms-sender send --input ./numbers.csv --template my-template --token 12345
sms-sender send --input … --template … --token … --smoke-test  # synchronous probe-send first
sms-sender send --input … --approval-test --test-number 09…    # send to your own #, prompt y/N
sms-sender preview --phone 09… --template … --token …           # show POST body, no API call
sms-sender preview --phone 09… --template … --token … --send    # actually send the one number
sms-sender retry-failed --input … --template … --token …        # reset failed_retriable + send
sms-sender --profile verify send --input …                        # load defaults from sms-sender.toml
sms-sender send --input … --rate 10/s                             # cap throughput (also 60/m, 3600/h)
sms-sender send --input … --notify slack:https://hooks.slack.com/… # post run summary on completion
sms-sender send --input trades.csv --template … \
    --token-column token10=first_name --token-column token=trade_side \
    --value-map trade_side:Buy=خرید --value-map trade_side:Sell=فروش  # per-recipient tokens from CSV columns
sms-sender status            # row counts by status
sms-sender export-failed     # dump failed_permanent to CSV
sms-sender dry-run --input … # parse + normalize, no API calls
sms-sender reset --status failed_permanent   # promote rows back to pending
sms-sender reconcile --state data/db/x.db    # ask Kavenegar about `unknown` rows (never sends)
sms-sender purge -y          # delete state DB (no undo)

# Tests
pytest                                   # full suite
pytest tests/test_state.py               # one file
pytest tests/test_state.py::test_claim   # one test
pytest -k "claim or in_flight"           # by name pattern
```

CI (`.github/workflows/tests.yml`) runs `pytest` on Python 3.10 (the `requires-python` floor) and 3.14 for pushes to `main` and every PR. Code must keep working on 3.10.

`KAVENEGAR_API_KEY` must be set (env or `.env` in cwd) for `send`. Other commands work without it. `SMS_SENDER_TEST_NUMBER` (optional) provides the default phone for `--approval-test`; the `--test-number` flag overrides it.

## Architecture

The whole pipeline exists to enforce one invariant: **a number that has been confirmed sent is never sent again, even across crashes** — and its corollary, **a number that *may* have been sent is never resent blindly**. Most non-obvious code is in service of that.

### Data flow

```
cli.send → make_runner → Runner.run
              │
              ├─ input_loader.load    (.txt/.csv → normalized phones + invalid rows)
              ├─ StateStore.upsert_pending / record_invalid / mark_orphans_unknown
              ├─ Runner._preflight    ← Sender.account_info + optional smoke send
              └─ ThreadPoolExecutor → Runner._send_one per phone
                       │
                       ├─ StateStore.claim(phone)     ← atomic pending→in_flight
                       ├─ Sender.send(phone)          ← Kavenegar + tenacity retry
                       └─ StateStore.mark_sent / mark_failed
```

`Runner` is the only place that knows about the state machine *and* the sender — the two are otherwise independent.

### End-of-run report ([runner.py](src/sms_sender/runner.py))

`RunSummary` carries `elapsed_sec`, `sends_per_sec`, and a `top_errors` tuple — the runner buckets every `PermanentSendError` / `SendError` message into a thread-safe `Counter[str]` keyed by `[code] message`, then emits the top 3 at the end. `tqdm`'s postfix shows `sent / fail / ok%` while running. `format_report(summary)` renders the human-readable block printed to stdout; `_summary_log_fields(summary)` flattens it for the structured logger. The `top_errors` keying intentionally collapses on message text (not phone) so the same misconfiguration shows up once with a count.

### Notifications ([notify.py](src/sms_sender/notify.py))

`notify(target, summary, error=None)` is best-effort and never raises. The `target` URL scheme picks the transport: `slack:<webhook>`, `telegram:<bot_token>:<chat_id>`, or plain `http(s)://` for a generic JSON webhook. The Slack/Telegram forms send the rendered report; the generic form posts a structured payload (`{"summary": {...}, "error": ...}`). On failure, a warning is logged with a redacted target — Slack webhooks and Telegram tokens are secrets and never appear in logs. Called once at the end of `cli._do_send`.

### Rate limiting ([rate.py](src/sms_sender/rate.py))

`TokenBucket(rate_per_sec, burst)` is thread-safe and used by `Runner._send_one` (called *before* `state.claim` so we don't lock the DB row while sleeping). `parse_rate` accepts `"10/s"`, `"60/m"`, `"3600/h"`, a bare number (per-sec), or `0`/`None`/`""` to disable. `Runner(rate_per_sec=0.0)` disables it (the default for tests). `--rate` and `--workers` are independent: workers control parallelism, rate caps total throughput.

### Profiles ([profile.py](src/sms_sender/profile.py))

`load_profile(config_path, profile_name)` reads a TOML file with `[profile.<name>]` sections, merging `[profile.default]` with the named profile (named wins). `to_default_map(values, commands)` expands the flat dict into Click's per-command `default_map`, so the same profile feeds every subcommand. The `cli` group is decorated with `--config` and `--profile` and sets `ctx.default_map` before dispatching to subcommands. Resolution order is **CLI flag > named profile > [profile.default] > built-in default**, which falls out of how Click consults `default_map` only when an option wasn't explicitly passed.

### Per-recipient tokens ([input_loader.py](src/sms_sender/input_loader.py))

`--token-column TOKEN=COLUMN` (repeatable) switches the loader to header-CSV mode: the first column is the phone, and each mapped column becomes that recipient's token (`LoadedRow.tokens`). `--value-map COLUMN:FROM=TO` translates values before sending. An empty cell, a value missing from its column's value map, or too many spaces for the token (`TOKEN_MAX_SPACES` in `sender.py`) makes the row invalid — raw text is never sent. `Runner` keeps `phone → tokens` from the input and passes them to `Sender.send(phone, tokens=…)`, where they layer over the static `SenderConfig` tokens; the CLI rejects a token set both ways. Tokens are **not** stored in the state DB, so a claimable DB row that isn't in the current input is skipped rather than sent without its tokens. The approval test borrows the tokens of the test number's own row, else the first recipient's. In a profile, use TOML lists: `token_column = ["token10=first_name"]`.

### Preflight (`Runner._preflight` → `_approval_test` → `_smoke_test_run`)

Before fan-out, three best-effort checks run in order. Any `PreflightError` aborts the run with `halted=True` and exit code 2.

- **Account check** (`Sender.account_info`) — calls Kavenegar `account/info`. A `HaltError` (401/403/418/etc.) aborts the run *before* any send. A non-halt `SendError` (e.g., transient network) just logs a warning and continues. Zero remaining credit also halts. Gated by `Runner.preflight` (default true).
- **Approval test** — opt-in via `--approval-test` + `--test-number 09…` (or `SMS_SENDER_TEST_NUMBER` env). Sends one SMS to the operator's own number out-of-band — bypassing the state DB, the rate limiter, and the executor (same model as `account_info`) — then prompts `y/N` at the terminal. Decline / EOF / closed stdin all abort. If the test send itself fails (Halt, Permanent, retries-exhausted), the run aborts *before* the prompt — there's no point asking the operator to approve something they didn't receive. Independent of `Runner.preflight`. **The test number is NOT removed from the recipient list** — if it appears in the input file it gets a normal in-band send too, by design.
- **Smoke test** — opt-in via `--smoke-test`. The first claimable phone is sent **synchronously** through the same `_send_one` path. If the outcome is anything other than `sent` (e.g., `PermanentSendError` for a missing template), the run aborts. The smoke phone consumes its DB row exactly once; subsequent fan-out skips it. Gated by `Runner.preflight` AND `Runner.smoke_test`.

The approval test runs *before* the smoke test on purpose: the operator gets a chance to manually decline before any auto-validated send happens. Tests inject a custom `approval_prompt` callable; the default uses `click.confirm` and treats `click.Abort` (closed stdin, Ctrl-C) as decline so CI is safe.

### State machine (owned by `state.py`)

Statuses: `pending`, `in_flight`, `sent` (= accepted by Kavenegar), `failed_permanent`, `failed_retriable` (= definitely not sent), `unknown` (= may have been sent), `needs_review` (= reconciliation couldn't decide). `CLAIMABLE = (pending, failed_retriable)`. The details that matter:

- **`claim()` is the dedup gate.** It runs `UPDATE … WHERE phone=? AND status IN CLAIMABLE`; if `rowcount == 0` the worker silently skips. Two workers racing on the same phone — one wins the UPDATE, the other gets `None`. `sent` and `failed_permanent` rows can never be claimed.
- **Connection-per-thread.** SQLite connections aren't shareable; `StateStore` keeps one per thread via `threading.local`. WAL mode + `BEGIN IMMEDIATE` keep concurrent writers from blocking each other badly.
- **Crash recovery via `mark_orphans_unknown`.** Any `in_flight` row at startup was left by a process that stopped mid-send (the run lock rules out a live one). Its request may or may not have reached Kavenegar, so it becomes `unknown` — never `pending` — and is never claimed again until checked with the provider. So you can `Ctrl-C` mid-run and re-run safely; the rest of the campaign carries on.
- **Every provider call is an `attempts` row** (kind `send` / `recovery`, outcome `accepted` / `retry` / `rejected` / `halt` / `unknown`, codes, `message_id`, `cost`, redacted detail). `Sender(on_attempt=…)` reports each call; `make_runner` wires it to `StateStore.record_attempt`. A failing audit write is logged and never changes the send's outcome. `recipients.cost` holds the accepted SMS's cost in rials.
- **One process per DB (`locking.RunLock`).** `Runner.run` and the commands that change rows (`retry-failed`, `reset`, `purge`) hold `fcntl.flock` on `<db>.lock`. A second process exits with code 2 instead of treating the first one's `in_flight` rows as crash leftovers and sending them again. The OS drops the lock when the holder dies (even `kill -9`), so crash recovery still works. `status` / `export-failed` only read and don't lock.
- **Invalid inputs are persisted with synthetic key `INVALID:<raw>`.** This keeps the `phone` PK constraint while letting `export-failed` surface them.
- **Schema versions.** `PRAGMA user_version` + append-only steps in `state._MIGRATIONS`. Opening a DB upgrades it in place, in one transaction; a DB written by a newer sms-sender is refused (`StateSchemaError`). Never edit a step that has shipped — existing DBs already applied it; add a new one.

### Reconciliation ([reconcile.py](src/sms_sender/reconcile.py))

`unknown` rows are settled by asking Kavenegar what it actually sent, never by resending. `reconcile_unknown` looks each phone up with `sms/statusbyreceptor` (`Sender.find_messages`) in a window around its last claim (−120 s … +900 s; Kavenegar allows ≤ 1 day). Message IDs the DB already accounts for (`known_message_ids`: sent rows plus recorded calls, e.g. the approval test) never count. Exactly one other message → `sent`; several → `needs_review`, which only `reset --status needs_review` (with confirmation) makes claimable. None → `needs_review` too, by default (`reconcile.REQUEUE_NOT_FOUND = False`): a live lookup on 2026-10-04 didn't find a 5-day-old lookup message, so "not found" isn't yet trusted as "never sent". With `requeue_not_found` (CLI `--requeue-not-found`, `Runner(reconcile_requeue_not_found=True)`) it becomes `failed_retriable`, claimable again; flip the default once a fresh lookup message is confirmed findable. Kavenegar reports an empty lookup as error 449, which `find_messages` turns into `[]`. Rows younger than the min age (300 s) wait. The settle methods only change rows that are still `unknown`, and each decision is an `attempts` row of kind `reconcile`.

The runner reconciles at the start of every run (so rows Kavenegar never got go out with everyone else) and at the end (long runs). It's best-effort: if the lookup fails — even a `HaltError` — the rows just stay `unknown`. `sms-sender reconcile` does the same standalone under the run lock; exit 1 while rows remain `unknown` / `needs_review`, 2 if Kavenegar refuses the lookup.

### Error taxonomy (split across `sender.py` + `classifier.py`)

`classifier.classify(code)` maps a Kavenegar status code to one of `{SUCCESS, RETRY, PERMANENT, HALT}`. `Sender._do_call` translates that into exception types:

| classifier action | sender raises | runner does |
|---|---|---|
| `RETRY` (409 / 451), or the connection itself failed (`_NotSent`: DNS, refused, connect timeout) | `_RetriableSendError` (internal) | tenacity retries up to `max_attempts`; if exhausted → `SendError` → `failed_retriable` |
| request may have been processed: read timeout, dropped connection, garbled 200 / 5xx reply, 200 with no entries | `UncertainSendError` | `unknown` — **never retried**, never claimable |
| `PERMANENT` | `PermanentSendError` | `failed_permanent` (skipped on resume) |
| `HALT` | `HaltError` | mark row `failed_retriable`, set `_stop` event, **abort the run with exit code 2** |
| `SUCCESS` | returns `SendResult` | `mark_sent` |

Unknown codes default to `PERMANENT` deliberately — don't burn credit looping on something we don't understand.

`tenacity` only retries `_RetriableSendError` (notice the leading underscore — it never escapes `Sender`). `HaltError`, `PermanentSendError` and `UncertainSendError` bypass retry by design. Which network errors count as "never sent" is decided in one place, `sender._never_sent`: only a failed TCP connection (possibly through the proxy). Everything later — and any bare `HTTPException` from an SDK — is uncertain. Each retry logs `send_retry` with the phone; since only never-sent calls and 409/451 are retried, these are not possible double sends.

### Why `_KavenegarHTTP` exists ([sender.py:92](src/sms_sender/sender.py#L92))

The packaged `kavenegar` SDK calls `requests.post()` with no timeout, so a hung connection would hang the worker forever. We POST directly via `requests` and re-raise the SDK's `APIException` / `HTTPException` types so the rest of the code is unchanged. If you swap or upgrade the SDK, preserve this wrapper.

### Phone normalization quirks ([phone.py](src/sms_sender/phone.py))

Canonical form is `09XXXXXXXXX`. The normalizer accepts `+98…`, `0098…`, `98…`, `9…`, plus arbitrary spaces/dashes/parens, and translates Persian (`۰۱۲۳`) and Arabic-Indic (`٠١٢٣`) digits to ASCII first — Excel exports often carry these. Anything else raises `InvalidPhoneError` and the input loader records it as an invalid row.

### Exit codes (set by `cli.send`)

| Code | Meaning |
|---|---|
| 0 | every recipient sent |
| 1 | run finished with some `failed_permanent`, `failed_retriable`, `unknown` or `needs_review` |
| 2 | `HaltError`, preflight failure, or declined approval-test aborted the run; or another process holds the state DB |

## Conventions

- **Recipient phone lists and state DBs are PII.** Keep state DBs in `data/db/` and segment / user-id lists in `data/segments/`; `data/` is gitignored as a whole, and so are root-level `*.csv` / `*.txt` / `*.xls*` (which catches `export-failed`'s default `./failed.csv`) and `*-numbers.*` / `*_numbers.*` anywhere. Never commit one — stage files by name, not with `git add -A` / `git add .`. When moving a DB, update every `state = …` in `sms-sender.toml` too: a path that no longer exists silently opens a fresh, empty DB, and that run re-sends to everyone already sent.
- Logs go to `logs/sms-sender.log` (rotating, 5 MB × 5) and are formatted as `key=value` pairs by `KeyValueFormatter`. Pass structured fields via `logger.info("event_name", extra={...})`, not f-strings, so they stay greppable.
- Adding a new Kavenegar status code: extend the relevant frozenset in `classifier.py`. Don't add per-code branching elsewhere.
