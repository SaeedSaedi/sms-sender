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
sms-sender send --campaign coin-price-7 --input …   # own DB: data/db/coin-price-7.db
sms-sender status            # row counts by status (+ campaign, template, last run)
sms-sender export-failed     # dump failed_permanent to CSV
sms-sender dry-run --input … # parse + normalize, no API calls
sms-sender reset --status failed_permanent   # promote rows back to pending
sms-sender reconcile --state data/db/x.db    # ask Kavenegar about `unknown` rows (never sends)
sms-sender delivery --campaign coin-price-7   # delivery reports for the last 48 h (never sends)
sms-sender check-sends --campaign coin-price-7  # did anyone get it twice? exit 1 if so (never sends)
sms-sender send --campaign coin-price-7 --input vip-2.csv --template … \
    --link-url https://kifpool.me/offer --link-token token3 \
    --user-id-column user_id                  # a short link per recipient, made before any SMS
sms-sender dry-run --input vip-2.csv --campaign coin-price-7 \
    --link-url https://kifpool.me/offer --link-token token3   # show a link's long URL, no Shlink call
sms-sender clicks --campaign coin-price-7     # click counts from Shlink, per segment (never sends)
sms-sender export-attribution --campaign coin-price-7   # ref → user ID for the backend, no phones
sms-sender export-clickers --campaign coin-price-7      # who clicked, with phones
sms-sender purge -y          # delete state DB (no undo)

# Tests
pytest                                   # full suite
pytest tests/test_state.py               # one file
pytest tests/test_state.py::test_claim   # one test
pytest -k "claim or in_flight"           # by name pattern
pytest -m e2e                            # browser tests: pip install -e ".[dev,web,e2e]" + Google Chrome
E2E_SHOTS=/tmp/shots pytest -m e2e       # + a screenshot of every page at every width
E2E_UPDATE_BASELINE=1 pytest -m e2e tests/web/e2e/test_pages.py   # rewrite baseline.json
pytest -m perf                           # a 100,000-recipient campaign's pages, each under 2 s
```

CI (`.github/workflows/tests.yml`) runs `pytest` and `pytest -m perf` on Python 3.10 (the `requires-python` floor) and 3.14, and `pytest -m e2e`, for pushes to `main` and every PR. Code must keep working on 3.10.

`KAVENEGAR_API_KEY` must be set (env or `.env` in cwd) for `send`. Other commands work without it. `SMS_SENDER_TEST_NUMBER` (optional) provides the default phone for `--approval-test`; the `--test-number` flag overrides it. Short links need `SHLINK_API_KEY` (`send --link-url`, `clicks`); `SHLINK_BASE_URL` defaults to `https://kifpool.me/u` and `SMS_SENDER_LINK_DOMAINS` (allowed destinations) to `kifpool.me`. The CLI loads `.env` from the current directory first thing (`cli` group callback), before any option or setting is read; it never overrides a variable that's already set. `tests/conftest.py` points `SMS_SENDER_LOG_FILE` (the CLI's `--log-file` default) at a temp folder, so no test writes to the real `logs/`. It also blanks every variable named in the real `.env` (it reads names only, never values) and sets a dummy Shlink key with an `.invalid` host. So no test can load a real key or test number, or reach the real Shlink. Keep it that way.

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

### Campaigns ([state.py](src/sms_sender/state.py) `meta`, [cli.py](src/sms_sender/cli.py) `_resolve_db_path`)

One state DB is one campaign. `--campaign <slug>` (every command that takes `--state`) picks `data/db/<slug>.db`; an explicit `--state` — flag or profile — wins, decided with Click's `get_parameter_source`. The `meta` table (schema v3) holds the campaign name, its settings, and the last run's numbers (`status` prints them; `status` never creates a DB).

Settings are what the campaign *sends* — `runner.campaign_settings`: template, static tokens, token columns, value maps — not workers, rate, timeouts or the input file (one campaign can be fed several segments). `StateStore.bind_campaign`, called first thing in `Runner._prepare`, records them on the first run. Later, different settings are accepted only while nothing can have gone out (no `sent` / `in_flight` / `unknown` / `needs_review` rows) — that's how a wrong template gets fixed — and otherwise only with `--allow-settings-change`. A mismatch raises `CampaignMismatchError` before any row is touched (CLI exit 2).

### Driving the runner without a terminal ([runner.py](src/sms_sender/runner.py))

`Runner.run()` holds the run lock and goes through `_prepare` (load input, seed state, settle leftovers) → `_preflight_checks` (account, approval test, smoke test) → `_fan_out`. It's built to be driven by the dashboard worker as well as the CLI:

- **Progress** goes to a `Reporter` (`note`, `start`, `advance`, `finish`). The default `TqdmReporter` is the CLI's bar and notes; tests and the worker pass their own. Library code never prints or calls `tqdm` directly.
- **`cancel()`** works from any thread. Nothing new is claimed, requests in flight finish and are recorded, and the rest stay claimable. Ctrl-C / SIGTERM do the same (a second one forces). The summary reports `stopped=True`, and the CLI exits 1 with a "re-run to continue" hint.
- **Signal handlers** are installed only with `install_signal_handlers=True` (the default) and restored when the run ends, so an embedding process keeps its own.

### End-of-run report ([runner.py](src/sms_sender/runner.py))

`RunSummary` carries `elapsed_sec`, `sends_per_sec`, and a `top_errors` tuple — the runner buckets every `PermanentSendError` / `SendError` message into a thread-safe `Counter[str]` keyed by `[code] message`, then emits the top 3 at the end. `tqdm`'s postfix shows `sent / fail / ok%` while running. `format_report(summary)` renders the human-readable block printed to stdout; `_summary_log_fields(summary)` flattens it for the structured logger. The `top_errors` keying intentionally collapses on message text (not phone) so the same misconfiguration shows up once with a count.

### Notifications ([notify.py](src/sms_sender/notify.py))

`notify(target, summary, error=None)` is best-effort and never raises. The `target` URL scheme picks the transport: `slack:<webhook>`, `telegram:<bot_token>:<chat_id>`, or plain `http(s)://` for a generic JSON webhook. The Slack/Telegram forms send the rendered report; the generic form posts a structured payload (`{"summary": {...}, "error": ...}`). On failure, a warning is logged with a redacted target — Slack webhooks and Telegram tokens are secrets and never appear in logs. Called once at the end of `cli._do_send`.

### Rate limiting ([rate.py](src/sms_sender/rate.py))

`TokenBucket(rate_per_sec, burst)` is thread-safe and used by `Runner._send_one` (called *before* `state.claim` so we don't lock the DB row while sleeping). `parse_rate` accepts `"10/s"`, `"60/m"`, `"3600/h"`, a bare number (per-sec), or `0`/`None`/`""` to disable. `Runner(rate_per_sec=0.0)` disables it (the default for tests). `--rate` and `--workers` are independent: workers control parallelism, rate caps total throughput.

### Profiles ([profile.py](src/sms_sender/profile.py))

`load_profile(config_path, profile_name)` reads a TOML file with `[profile.<name>]` sections, merging `[profile.default]` with the named profile (named wins). `to_default_map(values, commands)` expands the flat dict into Click's per-command `default_map`, so the same profile feeds every subcommand. The `cli` group is decorated with `--config` and `--profile` and sets `ctx.default_map` before dispatching to subcommands. Resolution order is **CLI flag > named profile > [profile.default] > built-in default**, which falls out of how Click consults `default_map` only when an option wasn't explicitly passed.

### Per-recipient tokens ([input_loader.py](src/sms_sender/input_loader.py))

`--token-column TOKEN=COLUMN` (repeatable) switches the loader to header-CSV mode: the first column is the phone, and each mapped column becomes that recipient's token (`LoadedRow.tokens`). `--value-map COLUMN:FROM=TO` translates values before sending. An empty cell, a value missing from its column's value map, or a value Kavenegar would reject makes the row invalid — raw text is never sent. Kavenegar's token rules (error 431) live in one place, `sender.token_problem`: at most 100 characters, no line break or `_`, and at most `TOKEN_MAX_SPACES[name]` spaces. They apply to static `--token…` values (CLI callback) and to every CSV row (loader). `Runner` keeps `phone → tokens` from the input and passes them to `Sender.send(phone, tokens=…)`, where they layer over the static `SenderConfig` tokens; the CLI rejects a token set both ways. Tokens are **not** stored in the state DB, so a claimable DB row that isn't in the current input is skipped rather than sent without its tokens. The approval test borrows the tokens of the test number's own row, else the first recipient's. In a profile, use TOML lists: `token_column = ["token10=first_name"]`.

### Preflight (`Runner._preflight` → `_approval_test` → `_smoke_test_run`)

Before fan-out, three best-effort checks run in order. Any `PreflightError` aborts the run with `halted=True` and exit code 2.

- **Account check** (`Sender.account_info`) — calls Kavenegar `account/info`. A `HaltError` (401/403/418/etc.) aborts the run *before* any send. A non-halt `SendError` (e.g., transient network) just logs a warning and continues. Zero remaining credit also halts. Gated by `Runner.preflight` (default true).
- **Account settings** (`Sender.account_config`) — right after the account check, a **GET without parameters** to `account/config` (any parameter would *change* that setting, so `_KavenegarHTTP.account_config` must never send one). `debugmode` on → `PreflightError` (nothing would be delivered); `resendfailed` on → a note (Kavenegar resends undelivered SMS once by itself). Unreadable settings never block.
- **Cost estimate** (`_check_credit`) — after the approval test: its `cost` (else the campaign's `StateStore.average_cost()`) × recipients, compared with the credit from the account check; more than the credit → `PreflightError`. Skipped while either number is unknown. `RunSummary.cost` sums what the run actually paid (approval test included).
- **Approval test** — opt-in via `--approval-test` + `--test-number 09…` (or `SMS_SENDER_TEST_NUMBER` env). Sends one SMS to the operator's own number out-of-band — bypassing the state DB, the rate limiter, and the executor (same model as `account_info`) — then prompts `y/N` at the terminal. Decline / EOF / closed stdin all abort. If the test send itself fails (Halt, Permanent, retries-exhausted), the run aborts *before* the prompt — there's no point asking the operator to approve something they didn't receive. Independent of `Runner.preflight`. **The test number is NOT removed from the recipient list** — if it appears in the input file it gets a normal in-band send too, by design.
- **Smoke test** — opt-in via `--smoke-test`. The first claimable phone is sent **synchronously** through the same `_send_one` path. If the outcome is anything other than `sent` (e.g., `PermanentSendError` for a missing template), the run aborts. The smoke phone consumes its DB row exactly once; subsequent fan-out skips it. Gated by `Runner.preflight` AND `Runner.smoke_test`.

- **Sending window** ([window.py](src/sms_sender/window.py)) — checked first. Default 08:00–21:00 Tehran (`--send-window`, env `SMS_SENDER_SEND_WINDOW`, `off`). Outside it `_preflight_checks` raises `PreflightError` (exit 2) before anything is sent; mid-run, `_send_one` stops claiming once it closes, and the run ends `stopped` (resumable). Independent of `Runner.preflight`. `Runner(clock=…)` makes it testable, and `tests/conftest.py` switches the window off so the suite doesn't depend on the time of day.

- **Test-only runs** (the dashboard's test step): `Runner(test_only=True)` needs `approval_test_number`. It runs everything up to the approval test and the credit check, then stops. Its link stage makes only the test SMS's link (`test:<phone>`); the recipients' are made when the send starts (plan 05 decision 4). It never prompts, runs no smoke test, sends to no recipient, and doesn't write `last_run`. A later send is given `cost_per_sms=` (the test's cost) for the credit estimate. `RunSummary` carries `credit`, `cost_per_sms`, `estimate` and `test_message_id`.

The approval test runs *before* the smoke test on purpose: the operator gets a chance to manually decline before any auto-validated send happens. Tests inject a custom `approval_prompt` callable; the default uses `click.confirm` and treats `click.Abort` (closed stdin, Ctrl-C) as decline so CI is safe.

### State machine (owned by `state.py`)

Statuses: `pending`, `in_flight`, `sent` (= accepted by Kavenegar), `failed_permanent`, `failed_retriable` (= definitely not sent), `unknown` (= may have been sent), `needs_review` (= reconciliation couldn't decide), `suppressed` (= on the opt-out list), `invalid` (= the input says don't send: two user IDs for one phone; only an explicit `reset --status invalid` undoes it), `capped` (= over the frequency cap this run; counted afresh next run). `CLAIMABLE = (pending, failed_retriable)`.

**Frequency cap** (`frequency.py`, `--frequency-cap N/DAYS`, plan 05 decision 6: off unless set):
- at most N accepted SMS to a number in DAYS days, counted by `state.folder_sends_since` across every campaign DB in the folder, query_only;
- what counts: accepted `send` calls and `reconciled_sent` decisions; test SMS don't;
- `Runner._apply_frequency_cap` runs with the exclusions in `_prepare`: `uncap()` first (the window moves), then `cap()` the claimable rows at or over N.

Capped people got nothing from this campaign, so freeing them again can't send anyone twice.

`--opt-out FILE` (repeatable) → `Runner(opt_out=…)` → `StateStore.suppress` turns matching claimable rows into `suppressed`. Rows already `sent` stay `sent`. Exclusions (opt-outs, user-ID conflicts) run in `_prepare` **after** orphan recovery and the start-of-run reconciliation, right before the queue is read. Otherwise a row that reconciliation requeues could slip past them in the same run. The details that matter:

- **`claim()` is the dedup gate.** It runs `UPDATE … WHERE phone=? AND status IN CLAIMABLE`; if `rowcount == 0` the worker silently skips. Two workers racing on the same phone — one wins the UPDATE, the other gets `None`. `sent` and `failed_permanent` rows can never be claimed.
- **Connection-per-thread.** SQLite connections aren't shareable; `StateStore` keeps one per thread via `threading.local`. WAL mode + `BEGIN IMMEDIATE` keep concurrent writers from blocking each other badly.
- **Crash recovery via `mark_orphans_unknown`.** Any `in_flight` row at startup was left by a process that stopped mid-send (the run lock rules out a live one). Its request may or may not have reached Kavenegar, so it becomes `unknown` — never `pending` — and is never claimed again until checked with the provider. So you can `Ctrl-C` mid-run and re-run safely; the rest of the campaign carries on.
- **Every provider call is an `attempts` row** (kind `send` / `test` (the approval test, via the `runner._ATTEMPT_KIND` context variable) / `recovery`, outcome `accepted` / `retry` / `rejected` / `halt` / `unknown`, codes, `message_id`, `cost`, redacted detail). `Sender(on_attempt=…)` reports each call; `make_runner(make_sender=…)` wires it to `StateStore.record_attempt`. A failing audit write is logged and never changes the send's outcome. `recipients.cost` holds the accepted SMS's cost in rials.
- **One kernel per folder (`sharing.py`).** The dashboard's worker writes `db/.dashboard-worker.json` (its kernel's boot ID) as it beats. A CLI on another kernel, such as a host outside Docker Desktop's VM where flock and SQLite's locks don't reach, exits 2 on any command that changes rows while that heartbeat is fresh, and warns on reads (`cli._guard_folder`). On one Linux kernel, host and containers share locks, so nothing is refused.
- **One process per DB (`locking.RunLock`).** `Runner.run` and the commands that change rows (`retry-failed`, `reset`, `purge`) hold `fcntl.flock` on `<db>.lock`. A second process exits with code 2 instead of treating the first one's `in_flight` rows as crash leftovers and sending them again. The OS drops the lock when the holder dies (even `kill -9`), so crash recovery still works. `status` / `export-failed` only read and don't lock.
- **Invalid inputs are persisted with synthetic key `INVALID:<raw>`.** This keeps the `phone` PK constraint while letting `export-failed` surface them.
- **Schema versions.** `PRAGMA user_version` + append-only steps in `state._MIGRATIONS`. Opening a DB upgrades it in place, in one transaction; a DB written by a newer sms-sender is refused (`StateSchemaError`). Never edit a step that has shipped — existing DBs already applied it; add a new one.

### Reconciliation ([reconcile.py](src/sms_sender/reconcile.py))

`unknown` rows are settled by asking Kavenegar what it actually sent, never by resending. `reconcile_unknown` looks each phone up with `sms/statusbyreceptor` (`Sender.find_messages`).

**How Kavenegar's lookup actually behaves** (checked live on 2026-10-04):
- It answers **per calendar day**. Any window inside a day returns that whole day's messages to the phone; other days return nothing.
- A fresh message is listed within a minute. Entries have no time field.
- `sms/select`, which has each message's time and text, answers 407 without an IP allowlist, so it isn't used.
- When the window around the claim (−120 s … +900 s) crosses midnight in Tehran or UTC, each day is also asked on its own (`reconcile._spans`).

**First, the row's own call records:** an accepted `send` attempt made after the row's claim (`StateStore.accepted_since`) settles it as `sent` with that message, without a lookup. That's the crash between Kavenegar accepting and `mark_sent`. The lookup can't settle it: the message ID is already known, so it would be skipped and the row requeued and sent again.

**Which messages count:** the candidates are the day's messages minus every known message ID. That means this DB's (`known_message_ids`: sent rows plus recorded calls such as the approval test) and every other campaign DB's in the same folder (`StateStore.neighbour_message_ids`, read-only), because another campaign may have texted the same person that day.

**Outcomes:**
- Exactly one candidate → `sent`.
- Several → `needs_review`. Only `reset --status needs_review` (with confirmation) makes it claimable.
- None:
  - attempt under `TRUST_NOT_FOUND_SEC` (24 h) old → `failed_retriable` (`REQUEUE_NOT_FOUND = True`; CLI `--review-not-found` turns it off);
  - older → `needs_review`, since a 5-day-old message wasn't listed.

**Other rules:**
- A message sent from the campaign account outside sms-sender on the same day could be taken for ours. The row then counts as `sent`: the error goes toward never sending twice.
- Kavenegar reports an empty lookup as error 449, which `find_messages` turns into `[]`.
- Rows younger than the min age (300 s) wait.
- The settle methods only change rows that are still `unknown`, and each decision is an `attempts` row of kind `reconcile`.

The runner reconciles at the start of every run (so rows Kavenegar never got go out with everyone else) and at the end (long runs). It's best-effort: if the lookup fails — even a `HaltError` — the rows just stay `unknown`. `sms-sender reconcile` does the same standalone under the run lock; exit 1 while rows remain `unknown` / `needs_review`, 2 if Kavenegar refuses the lookup.

### Double-send check ([sendcheck.py](src/sms_sender/sendcheck.py))

`sms-sender check-sends` counts each phone's SMS from the `attempts` rows, never from the recipient row, which only holds its last send:
- accepted `send`s and `reconciled_sent` decisions count, each message ID once;
- an `unknown` `send` / `recovery` is undecided until a reconcile decision. A `recovery` right after an accepted or unknown call is that same call.

Two or more IDs → `TWICE`; one plus an undecided call → `MAYBE`; either → exit 1. `test` attempts are counted apart, since the test number may also be a recipient. Older DBs filed the approval test as `send`, so there a test number that was also a recipient shows as twice. Sent rows without any attempt (from before schema 2) are reported as "unrecorded".

### Delivery reports ([delivery.py](src/sms_sender/delivery.py))

`sent` means Kavenegar *accepted* an SMS. `sync_delivery` asks `sms/status` (`Sender.delivery_statuses`: ≤ 500 message IDs per call) about sent rows from the last 48 h whose delivery status isn't final, and stores it in `recipients.delivery_status` / `delivery_checked_at` (schema v4). Final: 6, 10 (delivered), 13, 14, 100; 11 (undelivered) is re-checked because it can still turn into 10. Kavenegar only answers for 48 h, so `sms-sender delivery` has to run inside that window (the dashboard will schedule it). It doesn't take the run lock — it only writes the delivery columns — so it's safe during a send. `status` prints the breakdown (`delivery.describe`).

### Short links ([links.py](src/sms_sender/links.py), [shortlink.py](src/sms_sender/shortlink.py))

`--link-url` (needs `--campaign` and `--link-token`) turns on the link stage, `Runner._links_stage`, which runs after the account check and before the approval test. Invariants:

- **Every link before any SMS.** `LinkStage.run` returns tokens only when every link the run needs is `ready`; otherwise `LinkError` → `PreflightError` (exit 2), and nothing is sent. A test-only run needs only the test SMS's link. `ShlinkHaltError` (bad key, 401/403, a short URL that isn't `<base>/<code>`) stops it at once. A cancel during the stage ends the run `stopped`, not `halted`.
- **Rows before calls.** Each link is a `links` row (schema v6). The row holds the exact request — long URL, title, tags, `validUntil` — and is written before Shlink is called. A row that still matches the current settings is kept, so retries and resumed runs repeat the request byte for byte. That makes creation idempotent: Shlink's `findIfExists` (`ShortUrlRepository::findOneMatching`) matches longUrl **and** validUntil **and** the exact tag set, then returns the existing link. To give a link more time, `ShlinkClient.extend` PATCHes `validUntil` (done when it would expire within 24 h of sending); never create a new link for that.
- **Plans and keys.** `links.link_plan` fingerprints what shapes a link: destination, token, format, strategy, UTM values and campaign, but not expiry. Every row stores the fingerprint it was planned with (`plan`). Keys:
  - personal: the phone;
  - approval test: `test:<phone>`;
  - shared: `segment:<name>:<plan>` and `campaign:<plan>`.

  A personal or test row whose plan is stale is planned again (`StateStore.replan_links`). That's safe because only recipients still in the queue are asked for, so nobody has received the old link. Shared keys embed the plan, so a change creates new rows and the old ones stay for whoever got them.
- **`recipients.link_key`** records the link each recipient was claimed with (`StateStore.claim(phone, link_key=…)`). Reports and exports join on it, never on the phone, so they follow what each person actually received.
- **Retries are safe here, unlike SMS.** `ShlinkClient` retries network errors, read timeouts, 429 and 5xx with backoff, because a repeat is harmless. 4xx is `ShlinkPermanentError`: the link becomes `failed` and is asked again next run.
- **No personal data reaches Shlink.** A recipient's long URL carries the UTM parameters and `r`, a random 10-character reference from `secrets`. It is never derived from the phone or user ID: a hashed phone number can be reversed by trying every number. Titles and tags name the campaign and segment only. The `r` → phone/user ID mapping lives only in the campaign DB.
- **The link token is set before the claim.** `Runner._send_one` builds a recipient's tokens (CSV columns + link) before `state.claim`. A phone without its link is skipped, never claimed and never sent without it. The CLI rejects a link token that's also set statically or by a token column.
- **The approval test SMS gets its own link** (`test:<phone>`, tag `campaign-<slug>-test`, `utm_content=test`), so the operator's click never counts for a recipient or in the campaign's tag.
- **Campaign settings include the link settings** (`LinkSettings.as_settings`, but not expiry). Changing the destination, token, format, strategy or UTM values after a send is refused like any other settings change.

Destinations must be https on `SMS_SENDER_LINK_DOMAINS` (subdomains included), must not be a short link themselves, and must not already carry `utm_*` or `r` (`links.destination_problem`). Strategies: `recipient` (default; per-person clicks), `segment`, `campaign`. `--link-format code` puts only the short code in the token, for templates whose text already has `https://kifpool.me/u/`. A pattern with `{code}` (`links.token_value`, checked by `format_problem`) fits templates holding part of the URL, e.g. `u/{code}` after `https://kifpool.me/` (`introducecoin-c`). `--link-rate` (default 10/s) and 4 worker threads cap creation until Shlink's real speed is measured.

### User IDs and segments ([input_loader.py](src/sms_sender/input_loader.py), schema v5)

`--user-id-column COLUMN` reads a header CSV (first column = phone). A blank ID is still sent and reported as "missing user ID" (`LoadResult.missing_user_id`, `RunSummary`, `status`, exports). A phone with two different non-blank IDs is in `LoadResult.conflicts`: every one of its rows is invalid and it isn't sent. `Runner._apply_user_ids` also checks against IDs stored by earlier imports (`StateStore.assign_user_ids`) and takes conflicting phones out of the queue (`StateStore.exclude` → `failed_permanent`). Rows already `sent` are never touched. `--segment` (default: slug of the file name) is stored on each recipient (`upsert_pending(segment=…)`); it feeds `utm_content`, segment links and reports.

### Clicks ([clicks.py](src/sms_sender/clicks.py))

`sms-sender clicks` polls Shlink (`visits_by_tag("campaign-<slug>")`; Shlink has had no webhooks since 4.0) and stores each link's `nonBots` count on its row. It also keeps clicks per hour (`click_hours`, schema v7) from `ShlinkClient.visit_times` (`/tags/{tag}/visits`, bots excluded):
- it reads again from the start of the latest stored hour and counts every hour from there afresh (`replace_click_hours`), so a repeated sync never counts a visit twice;
- if Shlink can't say when, the counts stand and the hours wait. Updates go by an indexed `short_code`, in chunks of 500: `clicks` doesn't take the run lock, so it must never hold the write lock long enough to stall a send. Reports go per segment and join on `recipients.link_key`. Shared segment/campaign links count for the segment or campaign, never for a person. `export-attribution` (ref, user ID / "missing user ID", segment, link, ISO-8601 UTC time, delivery, clicks) has **no phone numbers**: it's for the backend. `export-clickers` has phones. Both write to `data/exports/` by default.

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

### Why `_KavenegarHTTP` exists ([sender.py:202](src/sms_sender/sender.py#L202))

The packaged `kavenegar` SDK calls `requests.post()` with no timeout, so a hung connection would hang the worker forever. We POST directly via `requests` and re-raise the SDK's `APIException` / `HTTPException` types so the rest of the code is unchanged. If you swap or upgrade the SDK, preserve this wrapper.

### Phone normalization quirks ([phone.py](src/sms_sender/phone.py))

Canonical form is `09XXXXXXXXX`. The normalizer accepts `+98…`, `0098…`, `98…`, `9…`, plus arbitrary spaces/dashes/parens, and translates Persian (`۰۱۲۳`) and Arabic-Indic (`٠١٢٣`) digits to ASCII first — Excel exports often carry these. Anything else raises `InvalidPhoneError` and the input loader records it as an invalid row.

### Exit codes (set by `cli.send`)

| Code | Meaning |
|---|---|
| 0 | every recipient sent |
| 1 | run finished with some `failed_permanent`, `failed_retriable`, `unknown` or `needs_review`; or it stopped early (`RunSummary.stopped`: Ctrl-C / SIGTERM / `Runner.cancel()`, or the sending window closed) |
| 2 | `HaltError`, preflight failure (incl. a link that couldn't be made, or Shlink refusing the key), or declined approval-test aborted the run; another process holds the state DB; or the DB belongs to another campaign / was sent with other settings |

### Dashboard (`sms_sender_web`, Phase 3, in progress)

The Django + HTMX dashboard sits in the same repo as the `web` extra; the CLI doesn't depend on it. `pip install -e ".[dev,web]"` installs it and its tests (pytest-django). Without the extra, the Django tests in `tests/web/` skip themselves. Fixtures (`tests/web/conftest.py`):
- `make_user(name, role)`;
- `viewer`;
- `signed_in` (a viewer, so it needs no second step);
- `verified(client, user)`: signed in and through the second step, with a linked app. It returns the TOTP device; get codes with `django_otp.oath.totp`.

- **Settings come from the environment** (`settings.py`, which also creates `DATA_DIR/db`, so a first start, e.g. in sandbox mode, can open its SQLite files; the CLI's `--state` paths are never created): `DJANGO_SECRET_KEY` is required, plus `DJANGO_ALLOWED_HOSTS`, `DJANGO_CSRF_TRUSTED_ORIGINS`, `SMS_SENDER_DATA_DIR` and `DJANGO_SECURE_COOKIES`. `.env` is loaded by `manage.py` and `wsgi.py`, **never by settings**, so the test session can't pick up real values. Tests run with `settings_test.py`: a dummy key and a temporary data dir.
- **Logs:** the engine's and the app's loggers, and `django.request` errors (a failed page, with its traceback), go to the console as `key=value` lines (`docker logs`).
  - Running gunicorn natively on macOS needs `OBJC_DISABLE_INITIALIZE_FORK_SAFETY=YES`: a forked worker crashes when a page calls Kavenegar or Shlink.
- **Data:** the app DB is `data/app.db` (SQLite, WAL, IMMEDIATE transactions, because the worker will write to it too). Campaign DBs are read through `StateStore` from `data/db/`, the same files as the CLI. Opening one upgrades its schema in place, as the CLI does.
- **Login on every page:** `LoginRequiredMiddleware`. Only views marked `@login_not_required` are open; for now that's `/healthz` and the login page.
- **Roles ([accounts/roles.py](src/sms_sender_web/accounts/roles.py)):**
  - A role is the Django group `viewer`, `operator` or `admin`; migration `accounts/0001_roles` creates them.
  - Each role adds capabilities to the one before it (`_ADDS`, the spec's permission matrix). Superusers are admins.
  - Only people who can `run_campaigns` keep a number for test SMS (My account hides it from viewers and refuses their POST).
  - Gate views with `@requires("capability")`. It renders the Persian `403.html`, which also says when the user has no role yet.
  - In templates, use `user|can:"capability"`.
  - `role_of` caches the role on the user object, and `set_role` clears it.
  - Never check group names directly.
- **Two-step verification (`django-otp`, TOTP):**
  - `TwoFactorMiddleware` sends a signed-in operator or admin to `/2fa/` (or `/2fa/setup/` if they have no app yet) until this session has passed the code. Only the names in its `EXEMPT` are reachable before that.
  - Setup works only while the user has no confirmed device. A linked app is replaced only by an admin's reset, never from a session that has just the password.
  - A passed code cycles the session key.
  - Throttling after wrong codes, and refusing a reused code, come from django-otp.
  - Viewers never see these pages.
- **Users (admins):**
  - a temporary password (`reset_password`, checked by the password rules; their sessions end with it);
  - `Profile.must_change_password`, also offered when creating an account;
  - `PasswordChangeMiddleware`, after `TwoFactorMiddleware`, sends a flagged person to `/password/` until they choose their own;
  - last activity, from the activity log.
- **Activity log (`audit` app):**
  - Call `audit.record.record(action, request=…, campaign=…, **detail)` for anything a person does. `action` must be in `record.ACTIONS`. Give each action a Persian label in `audit/terms.py`, and a line in `describe()` if it has details.
  - Pass `username=` instead of a user for someone who isn't signed in.
  - Sign-in, sign-out and failed sign-in are recorded from Django's signals (`audit/apps.py`). The username tried is recorded, never the password.
  - `ip` is `REMOTE_ADDR`, and no proxy header is trusted. Behind Docker Desktop it is Docker's gateway, not the person's own IP.
  - Events are append-only. `/activity/` (`view_audit_log`) shows them, 100 to a page. It filters by action, person, campaign and period, and `/activity/export.csv` downloads what's filtered (recorded as `audit_exported`). Phone numbers are masked there too.
- **Persian (spec 4.11):**
  - Templates use `{% translate "English id" %}`, with the Persian in `locale/fa/LC_MESSAGES/django.po`. After any change to the `.po`, recompile with `msgfmt -o django.mo django.po`.
  - `tests/web/test_catalog.py` fails on:
    - missing, empty, fuzzy or stale entries;
    - Arabic «ي»/«ك»;
    - a space or nothing where the glossary has a half-space, including after «می»/«نمی».

    It extracts `{% translate "…" %}` and `_` / `gettext` / `gettext_lazy` calls (adjacent literals are joined). `{% blocktranslate %}` isn't extracted, so avoid it.
  - The catalog also rewords Django's own messages that a page can show: password rules, and the password-change errors. Django's Persian mixes «رمز عبور» with «گذرواژه». It comes first because it's in `LOCALE_PATHS`. Django 5.2's short-password message id uses `%d`, and Django's own Persian catalog still has the old `%(min_length)d` id. Without our entry that message is English. When upgrading Django, check those ids still match.
  - Error pages: `403.html`, `403_csrf.html`, `404.html` and `500.html`. `500.html` extends nothing, because it's rendered without a request.
  - Form errors go through `|fa_digits` (Django fills in Latin digits, e.g. the minimum password length).
  - Identifiers (slugs, template names, user IDs) go through `|copyable`. It renders the value, with Persian digits if it's purely numeric, and a copy button holding the exact stored value. The button falls back from the clipboard API, which needs HTTPS, for plain-HTTP NetBird access.
  - Text people type (names, notes, fixed token values, value translations) goes through `text.persian_text`: Arabic «ي»/«ى»/«ك» become «ی»/«ک».
  - Kavenegar's codes come with their meaning (`campaigns/terms.KAVENEGAR_CODES`, `code_meaning`). A reason with `code=None` reads "Kavenegar didn't answer".
  - Confirmations state the consequence with the real number, e.g. how many recipients a cancel cancels.
  - `test_catalog.py` also fails on stale entries, so the specialist reviews only text that's shown. Django's own reworded messages are listed in `DJANGO_OWN`.
  - Status names live in `dashboard/terms.py`, worded as in the spec's glossary.
  - Show numbers with the `fa` filters: `fa_number`, `fa_digits`, `jalali` (Solar Hijri, Tehran time) and `ltr` (`<bdi dir="ltr">` for values with separators or Latin letters).
  - Never put Persian digits inside links or codes.
- **Segments (`segments/`):**
  - An upload is stored as `data/segments/<slug>.upload` until its columns are chosen. Then it becomes the prepared copy, `data/segments/<slug>.csv`, in the CLI's own input format: UTF-8, commas, the phone first under the header `phone`, then the user-ID column and the token columns under their own names. The upload is deleted.
  - Every row is kept, invalid ones too, so a send records them like the CLI does. The engine reads the prepared copy unchanged.
  - `files.parse` reads UTF-8 (with or without a BOM), UTF-16 and Windows-1256, with `,` `;` or tab separators, and refuses `.xlsx` / `.xls` by their magic bytes. Windows-1256 has no Persian «ی», so text read that way gets the Arabic «ي»/«ى» turned into «ی».
  - `files.summarize` counts with `input_loader.load`, exactly as a send would. Its invalid-row sample is stored masked.
  - The slug follows the CLI's `--segment` rules (`SLUG_RE`). `upload` is reserved, because of `/segments/upload/`.
  - A segment that a `Campaign` names in `settings["segment"]` can't be deleted.
  - **Actions:**
    - start a campaign from it (`/campaigns/new/?segment=`);
    - download the prepared copy (`export_people`, with a BOM, recorded);
    - replace the file (`segment_replace`).
  - **Replacing** is refused once any campaign DB may have sent from it (`StateStore.segment_may_have_sent`), or while a send that uses it is on its way. The new file goes through the columns step again. `Segment.version` goes up, and `services.settings_hash` includes it once it's above 0, so campaigns using the segment need a new test SMS and a queued send is withdrawn.
  - The upload → columns → summary pages share a stepper (`segments/_steps.html`).
- **Operations (`system/operations.py`, admins):**
  - `/system/backups/`: back up now (`keep=None`), list from the manifests, check one (`verify`). Restoring stays on the server, with both services stopped.
  - Purge (`campaign_purge`, `delete_campaign_data`, the CLI's `purge`): the typed short name, refused while any of the campaign's jobs is on its way, a backup first, then the DB files deleted under the run lock, and the `Campaign` with them.
  - Adopt (`campaign_adopt`, operators, from the home page): a `Campaign` for a DB the CLI made, its settings from the DB's bound ones. With no segment it's a draft. As it has sent, its message is fixed, so the draft offers the next segment like another round.
  - The status page shows the version, the queue and the last backup.
- **System settings (`system/`, `/system/`, `manage_settings`):** one `SystemSettings` row. Changes are recorded as `system_settings_changed`.
  - The frequency cap: `Engine.runner` passes it to every run, and `checks.check_campaign` counts it (`capped`, `cap`).
  - Notification targets (the CLI's `--notify`): `worker._announce` sends each one the run report with `heading=<slug>` when a send ends or stops (not for tests, not on a shutdown requeue). They're secrets: pages and the log only show `notify.redact_target`. There's a "send a test" button (`notify.notify_text`).
  - Defaults for new campaigns: the window and the rate.
- **Suppression list (`suppression/`):**
  - A `Suppression` row is a canonical phone, either global (`campaign` null) or for one campaign. Two partial unique constraints keep each number once per scope, because NULLs never collide in a plain UNIQUE.
  - `service.add` and `service.phones_for(campaign)` are the API. `jobs.engine.Engine.runner` passes `phones_for(campaign)` into the runner's `opt_out`, so every send skips them.
  - Operators (`add_suppression`) see the page and add numbers, pasted or from a file. They apply to every campaign, or to one (the form's `scope`, the CLI's `--opt-out`). Only admins (`remove_suppression`) remove them. Both changes are audited.
  - A number is searched with a POST (`action=find`), never in a URL.
- **Phone numbers on pages** are masked with `privacy.mask_phone` (filter `mask_phone`): the first four and last two digits are shown, the rest become `*`. Values with fewer than ten digits are hidden entirely. Use `*`, never `•`, because next to Persian digits a dot reads as «۰».
- **`accounts.decorators.forbidden(request)`** renders the Persian 403. Use it for checks inside a view, such as an action only admins may take.
- **Campaign pages (`campaigns/`, spec 3):**
  - Settings are stored in `Campaign.settings` in the CLI's terms: `segment`, `input`, `user_id_column`, `template`, `tokens`, `token_columns`, `value_maps`, `links` (LinkSettings fields), `send_window`, `rate`, `workers`. The engine (`jobs/engine.Engine.runner`) turns them into the CLI's runner.
  - The flow: check (`checks.check_campaign`: read only, no API, never creates the campaign DB) → test SMS (`Job.Kind.TEST`) → approval (`services.decide_test`) → send (`services.start_send`).
  - A test job runs the runner with `test_only=True`: prepare, window, account checks, the test SMS's own link, then the approval test to the requester's `Profile.test_phone`. It stops there, without recording a "last run".
  - A send needs `services.approval(campaign)`: the latest test, approved, with the same `settings_hash`. The hash covers `APPROVED_SETTINGS` (segment, template, tokens, token columns, value maps, links without expiry). It doesn't cover the window, rate or workers. The send gets the test's `cost_per_sms` for the credit estimate.
  - **Settings lock** (`services.settings_locked(campaign, user)`):
    - `scheduled` while a send waits for its time (cancel the schedule first);
    - `started` once an SMS *may have gone out* (`StateStore.may_have_sent`: a `sent`, `in_flight`, `unknown` or `needs_review` row; the same rule as `bind_campaign`);
    - `waiting` while a send is on its way and nothing has gone out yet.

    A send that failed before sending anything locks nothing, so a wrong template can still be fixed. The page and its POST both refuse.
  - **Rounds:** a finished send whose `settings_hash` isn't the campaign's current one belongs to an earlier round (`lifecycle`). The campaign then starts again from the check and the test SMS. Three ways lead there:
    - **Another segment** (`campaign_segment`, operators): only `segment`, `input` and `user_id_column` change, and only to a ready segment that has every column the tokens use. Offered in the completed and cancelled steps. The campaign DB skips anyone already sent; rows of the old list that aren't in the new input wait.
    - **An admin's changed message** (`campaign_unlock`, capability `change_sent_message`, the CLI's `--allow-settings-change`). It needs the typed short name, then `services.unlock_message`:
      - a full backup first (`make_backup(keep=None)`, which prunes nothing); if it fails, nothing changes;
      - a paused send is superseded (`CANCELLED`, `result.superseded`, nobody cancelled);
      - `Campaign.unlocked_at` / `unlocked_by` are set.

      Until a send starts after it (`message_unlocked`), the settings are open to admins only, with a warning. New test and send jobs carry `params.allow_settings_change`, and the worker passes it to the runner, so `bind_campaign` accepts the change.
    - **A changed setting while nothing has gone out.**

    The lifecycle skips withdrawn and superseded sends.
  - **Queue again** (`campaign_requeue`, the CLI's `reset --status` / `retry-failed --include-permanent`): `services.REQUEUE` maps each status to its capability.
    - Rejected: an operator (`update_campaigns`).
    - Unknown, needs review, suppressed, invalid, cancelled and sent: an admin (`requeue_review`). Sent also needs the typed short name and a backup first (`keep=None`).
    - Refused while a send is on its way. It holds the run lock.
    - The confirmation states the number and the consequence (`terms.REQUEUE_CONFIRM`); the activity log records `recipients_requeued`.
    - Not-sent rows are already in the queue: "send to them again" covers them.
    - `StateStore.reset_status` never queues an `INVALID:<raw>` row.
  - **Reconcile options:** the CLI's `--min-age` (in minutes) and `--review-not-found` go in the reconcile job's `params` (`min_age_sec`, `requeue_not_found`). The worker passes them to `reconcile_unknown`.
  - **Duplicate** (`campaign_duplicate`, the dashboard's presets: the CLI's `--config` / `--profile`): a new campaign with a deep copy of the settings, and a suggested short name from `forms.free_slug` (coin-price-7 → coin-price-8; CLI DBs count as taken). No tests, approvals or sends are copied.
  - **Approval guard:** on a send's first claim (`attempts == 1`), the worker compares its `settings_hash` with the campaign's. A mismatch sends nothing: the job is withdrawn (`CANCELLED`, `result` `{"withdrawn": True, "stop_reason": "not_approved"}`, `started_at` cleared), and the campaign is back at the test SMS. A send without the hash never runs, so tests that queue one directly give it `services.settings_hash(campaign)`.
  - **Scheduling:** `services.start_send(campaign, user, at=…, smoke_test=…)`. `Job.not_before` keeps a queued send from being claimed until then. `forms.parse_when` reads a Solar Hijri date and an HH:MM Tehran time (Persian digits work). It refuses a Gregorian-looking year (≥ 1900), the past, and more than `SCHEDULE_MAX_DAYS` (30) ahead. A wrong one comes back on the page with what was typed. `unschedule` withdraws the send (`result.withdrawn`, nobody cancelled); `start_now` clears `not_before`. The lifecycle skips withdrawn sends.
  - **Smoke test:** `params.smoke_test` → `Engine.runner(smoke_test=)` → the CLI's `--smoke-test`.
  - **Cost before the test:** a test job records its message's `params.parts` (from the template library). `preview.price_per_part()` is the latest such test's `cost_per_sms` divided by its parts. The previews show `parts × price` per recipient, and the total in the ready step, as an estimate.
  - **Ready step:** `_live()` adds the check (as `present.checklist`: one ✓ / ⚠ / ✗ line per finding, its state also in hidden text), the message and `preview.recipients` whenever the stage is READY, so the poll renders the same step. Only `run_campaigns` gets each recipient's message and the number search. The search is a POST to `campaign_preview` (no number ever goes in a URL), answered with the `#recipients-preview` fragment for HTMX. Viewers see the message only.
  - `services.JobConflict.code` picks the Persian message in `campaigns/terms.CONFLICTS`.
  - The live part (`_live.html`) polls `/campaigns/<slug>/live/` every 3 s with HTMX, only while a job is active.
- **Reports (`reports/`, read only):**
  - `/reports/<slug>/` reads any campaign DB in `data/db/`, the CLI's too, and never creates one.
  - It uses the CLI's own queries: `click_report`, `display_counts`, `delivery_counts`, `total_cost`, and `recipients_page` (by rowid, so no number appears in a URL).
  - **Charts** (`reports/charts.py`, server-drawn, no script):
    - the funnel: accepted → delivered → clicked (clicked only with links of one's own), as `<progress>` bars;
    - clicks over time: SVG bars, per hour up to `HOURLY_UP_TO` (72), else per day (Tehran). Time runs right to left. Coordinates are formatted in Python, so no locale's decimal mark reaches an SVG attribute. A table holds the same numbers.

    `/analytics/` puts every campaign DB side by side (the CLI's too): accepted, delivered, clicked, clicks, cost, cost per click.
  - **Audiences** (`segments/audience.make_audience`, operators): a new ready segment from the recipients the list's filters match.
    - Ready-made filters: clicked, didn't click, delivered but didn't click, not delivered, rejected. Only those with anyone in them are offered.
    - Numbers and user IDs come from the campaign DB.
    - Token columns come back from each source segment's file: only the columns every source has, and none if any source file is gone, so no row gets an empty token cell.
  - **Conversions** (`sms_sender/conversions.py`, schema v8 `conversions`, operators with `update_campaigns`): a CSV with `r` and/or `user_id`, optional value and converted_at.
    - Matched to a recipient who was sent the SMS: by r first (their own link), then by user ID.
    - The same person, value and moment isn't added twice.
    - The report shows people converted (of the accepted), conversions and value; the funnel gains "Converted". No phone numbers are needed or stored from the file.
  - **"Did anyone get it twice?"** (`check-sends`) runs `sendcheck.check_sends` on the report, for viewers too: twice, maybe, or OK, with masked numbers. Calls from before schema 2 are counted apart.
  - **The recipients list** filters with `state.RecipientFilter`, shared by `recipients_page`, `recipient_total` and `iter_recipients`. The filters:
    - status, as `display_counts` names it (`invalid` included);
    - segment;
    - delivery: a `state.DELIVERY_GROUPS` key, or `UNCHECKED`;
    - clicked: only someone's own link counts;
    - missing user ID.

    Filters go in the address, with nothing personal in them. A number is searched with a POST, never a GET. A filter is offered only where it means something: several segments, personal links, user IDs. "Missing user ID" shows only where user IDs are used.
  - Numbers are masked. `reveal_phone` (operators) POSTs a rowid to `/reveal/` (HTMX swaps the cell) and records `phone_revealed`. Invalid input rows (`INVALID:…`) are never revealed.
  - Downloads use English column names, Latin digits and a UTF-8 BOM. They're the same rows as the CLI's `export-attribution` / `export-clickers`. Operators (`export_people`) also get:
    - `recipients.csv`: the list with its filters, numbers in full;
    - `failed.csv`: the CLI's `export-failed`, with `StateStore.FAILED_HEADER` shared by both.

    Each download records `report_downloaded`, with the filters (never a number).
  - `/status/` asks Kavenegar (`account_info` / `account_config`) and Shlink (`health`) live, on every view.
  - Worker liveness is `jobs.WorkerBeat`: written every idle loop and with each job heartbeat. `worker_alive()` means seen within 60 s.
- **The attribution API (`api/`, plan 05 decision 8):**
  - Read only: `GET /api/v1/campaigns/` and `/api/v1/campaigns/<slug>/attribution/?page=N`. The rows are `clicks.attribution_rows`, 1,000 a page, never a phone number.
  - `api_view` makes a view `@login_not_required`, CSRF-exempt, never cached and GET only. It checks `Authorization: Bearer <token>` on every call: 401 with `WWW-Authenticate`, else the view.
  - Tokens (`api.tokens`): `smsk_` + 43 random characters, shown once. Only the SHA-256 is stored (`ApiToken.digest`), with its first 10 characters as `prefix`. They're revoked, never deleted.
  - Admins (`manage_settings`) issue and revoke them on `/api-tokens/`.
  - Every call records `api_called` with `username="api:<token name>"`.
- **Sandbox (`jobs/sandbox.py`, `SMS_SENDER_SANDBOX=1`):**
  - `Engine.sender()`, `Engine.link_client()` and `Engine.runner()` return `SandboxKavenegar` / `SandboxShlink`. `make_runner(make_sender=SandboxKavenegar)` builds the simulated sender, so no API key is read and no request is made. It reports each call to `on_attempt` like the real one (accepted / rejected / unknown), so `check-sends` works on sandbox campaigns.
  - `settings.DATA_DIR` becomes `<data>/sandbox`, the app DB included. A worker not in sandbox mode reads another DB and can't see a sandbox job.
  - The simulation is deterministic, so tests can rely on it:
    - each link's visits come at fixed moments within 36 hours of its making (`visit_times`), as time passes;
    - numbers ending in `000` are rejected (411);
    - `999` are accepted but raise `UncertainSendError` (the reply is lost);
    - message IDs ending in 7 are undelivered;
    - clicks are 0–3 per link, by a hash of the code.
  - Every accepted SMS, with its final tokens (static, then the row's), is a line in `<DATA_DIR>/sandbox-outbox.jsonl` (`read_outbox()`). `find_messages` answers from it, as Kavenegar's `statusbyreceptor` would, so reconciliation settles a lost reply as `sent` without a resend. The status page shows the latest entries.
  - The `sandbox` context processor drives the banner on every page.
- **Link-stage progress:**
  - `LinkStage(progress=…)` reports `(done, total)`. The runner passes `reporter.links` when the reporter has one; the CLI's doesn't.
  - `JobReporter.links` stores `{"stage": "links", "total", "processed", "eta_sec"}` on `Job.progress`. The campaign page shows a bar and the time left, since Shlink is capped at 10 links a second (about 18 minutes for 11,000).
- **Engine messages in Persian (spec 4.11):**
  - The engine writes English for the CLI and the logs. The dashboard renders keys, so engine English never appears on a page.
  - `PreflightError(message, key, **fields)` → `RunSummary.stop_reason` / `stop_fields`. That includes `provider_halt` with Kavenegar's code, and `window_closed`. The worker adds `busy`, `settings_mismatch`, `input_unreadable`, `crashed` and `given_up` in `Job.result`.
  - `sender.token_issue`, `links.destination_issue` and `InvalidRow.key` are the keyed forms of the CLI's messages. `token_problem` / `destination_problem` word them in English.
  - `campaigns/terms.py` holds the Persian for every key. Add a key there, and in the catalog, whenever the engine gains one.
  - Notes too: `Reporter.note(text, key, **fields)`. The English text is for the CLI and logs. `JobReporter` stores `key` and `data` on a `JobEvent`, and the history shows `terms.NOTES[key]` (`campaigns/present.notes`). A note without a key is never shown.
- **Campaign lifecycle ([lifecycle.py](src/sms_sender_web/campaigns/lifecycle.py), plan 05 P2):**
  - One derived stage, never stored, from the settings and the latest test and send jobs:
    - `draft` → `ready` → `testing` → `awaiting` → `approved`;
    - then `scheduled` / `sending` / `paused` / `stopped` → `completed` / `cancelled`.
  - A test newer than the latest send takes over.
  - `step` (1–5: message and list, check and preview, test SMS, send, results) drives the stepper, and `tone` the pill.
  - The campaign page includes `campaigns/stage/_<stage>.html` as its current step. `present.py` turns jobs into Persian: `result_line`, `notes`, `top_errors`, `send_summary`.
  - The overview (`dashboard/views.home`) counts stages into tiles and lists what needs attention: tests to approve, stopped sends with their reason, and leftovers after a send.
  - **Sending window:** a send the window stops (`stop_reason` `window_closed` or `outside_window`) is `paused`, and `Worker.resume_when_window_opens` (every loop) queues it again once the window is open. An operator's pause is never resumed for them.
- **Settings page, templates, preview (plan 05 P2):**
  - The page's split controls (window from/until, rate number + unit, link-format choice + pattern, translation rows `vm_column`/`vm_source`/`vm_target`) are turned back into the form's combined fields by `forms.combined()` before validation. The CLI-shaped fields still work, so the validation is unchanged.
  - Persian digits are accepted.
  - UTM values go into `links` (part of the approved settings).
  - `max_attempts`, `timeout`, `backoff_max` and `link_rate` are top-level settings. Only an admin (`manage_settings`) can change them; the form ignores them from anyone else.
  - A campaign's `timeout` also applies to its follow-up jobs (`Engine.sender(campaign)`, `link_client(campaign)`).
  - **Template library** (`campaigns.MessageTemplate`, `/templates/`): a copy of each Kavenegar template's text, used only to preview.
  - `campaigns/message.py` fills `%token…` placeholders (longest first), counts length like the networks (GSM-7 160/153, UCS-2 70/67) and finds placeholders nothing fills or tokens the text doesn't use. `shows_left_to_right` flags Persian text whose first letter is Latin: phones take the first strong character, so it would show left to right. `preview.py` fills them with the first valid row (after translations) and a sample link (`SAMPLE_CODE`).
  - The settings page previews live: HTMX posts the unsaved form to `settings/preview/`, which saves nothing.
  - `{% translate %}` doubles a `%` before the lookup, so a template string with `%` always shows in English. `test_catalog` refuses one; put such text in Python, or reword it.
- **Jobs and the worker** (`jobs/`, spec 4.6 / 4.8). `Campaign` holds a campaign's send settings, in the CLI's terms; its `slug` names `data/db/<slug>.db`. `Job` kinds: send, reconcile, delivery, clicks. `JobEvent` holds the engine's notes.
  - **Claiming:** `Worker.claim(kinds)` takes the oldest queued job of those kinds, or a running one whose lease expired, with an atomic UPDATE. A heartbeat thread renews the lease every 10 s and reads `Job.control`.
  - **Two lanes** (`run_forever`, `LANES`): one thread runs sends, one at a time, which the frequency cap and Kavenegar's rate count on. Another runs `SHORT_KINDS` (test, reconcile, delivery, clicks) beside it, so an urgent test SMS never waits behind an hour-long send. The main thread keeps the heartbeat, the schedule and the window's resumes.
  - A job that holds its campaign's run lock (`LOCKING`: send, test, reconcile) isn't claimed while another one of that campaign holds it with a live lease. Delivery and clicks may run beside a send. `run_once(kinds=None)` (tests, `run_worker --once`) claims any kind.
  - **Stopping:** pause, cancel and SIGTERM all end in `Runner.cancel()`. Afterwards, a paused job waits, a cancelled one runs `StateStore.cancel_remaining()` (claimable → `cancelled`), and an interrupted one goes back to queued.
  - **Giving up:** after `MAX_ATTEMPTS` lost leases a job fails.
  - **Operator actions** are in `jobs/services.py`: enqueue (one active job per campaign and kind), pause, resume, cancel. Cancelling a job that isn't running takes the campaign's run lock.
  - **Engine:** `jobs/engine.Engine` builds the CLI's runner from `Campaign.settings` (`make_runner(reporter=JobReporter, install_signal_handlers=False)`); tests swap in fakes. The approval test isn't part of a send job; it becomes its own dashboard step.
  - **Scheduler:** `Worker.schedule` queues delivery updates while sent rows are under 48 h old, and click updates for 14 days.
  - **One worker process only** (with its two lanes). Test DBs are files, not shared-memory SQLite (`settings_test`), because the heartbeat and lane threads write concurrently.
- **Design system v2 ([app.css](src/sms_sender_web/static/css/app.css), the review's R3):** an operations console, calm and status-first. It's built on v1 (P1, below), and v1's class names still work.
  - **Shell:** a deep-navy full-height sidebar (`--nav-*` tokens). Its groups are: SMS sending; reports and monitoring; administration. Help sits by the user. The active item is `[aria-current]`, any value.
  - **`.figures`** is a stat strip: one panel whose items share the width (`flex: 1 1 8rem`), so a short row never leaves a lonely box. Clickable figures are `ul.figures > li > a.figure-link` with `.figure-label` / `.figure-value`, never links inside a `<dl>`.
  - **Status at a glance:**
    - `.statusbar` holds one `<span class="sb-<tone>" style="flex-grow: N">` per status (`status|status_tone`, `dashboard/terms.STATUS_TONE`), `aria-hidden`;
    - beside it, `.legend` gives every status's name and count, so the numbers are always in text.
  - **Campaign page** (`_live.html`): `.work`, with `.work-main` (stage, stepper, current step, history as a `.timeline`) and `.work-side` (what it sends; recipients). The ids the poll and the tests use stay: `#live-status`, `#stage`, `#stepper`, `#current-step`, `#recipients-step`, `#history`.
  - **Report tabs** (`[data-tabs]`, app.js `setupTabs`): `role=tablist` links to panels (`[data-tab-panel]`).
    - Without a script every panel shows. With one, a panel at a time, arrow keys mirrored for RTL.
    - `#recipients`, or any anchor inside a panel, opens that panel.
    - Django tests see the whole page; browser tests click the tab first.
  - **Messages** (`ui/messages.html`): errors and warnings stay on the page (`.messages`). Success and information are `.toasts` that leave after 8 s, held while pointed at or focused.
  - **Forms:** `.form-section` for a form in sections (title and help beside the fields). Fieldset legends read as titles inside the box.
  - **Other parts:** `.tabs` / `.segmented-tabs`, `.toolbar`, `.timeline`, `.health-grid` / `.health-state` (status page), `.auth-card` (sign-in), `.chip` (meta).
  - **Overview** (`dashboard/views.home`): tabs (all, in progress, needs attention, ended), stage tiles that filter (`?stage=`), a search by name, short name or template (`?q=`, nothing personal), 50 to a page, at most 5 attention items with "show all".
  - **Settings:** when the template's text is in the library, only the tokens it uses show (`campaigns/_token_row.html`). The rest fold under "the tokens the text doesn't use"; a filled one stays in view.
- **Design system v1 ([app.css](src/sms_sender_web/static/css/app.css), plan 05 P1):**
  - **Basics:**
    - Plain CSS, no build step. Logical properties only, so the layout mirrors for RTL.
    - Tokens are custom properties, with a dark mode under `prefers-color-scheme`.
    - Older class names (`button.secondary`, `.figures`, `.badge`, …) map onto the same components.
    - HTMX, idiomorph (0BSD) and the Vazirmatn font (OFL) are vendored in `static/`; no CDNs.
    - Ordered lists use `list-style-type: persian`.
    - No inline `onclick` / `onsubmit`; behaviour lives in `static/js/app.js`.
  - **Shell:**
    - `base.html`: a skip link, a sidebar from 1024 px and a `<dialog>` drawer below, the sandbox banner as a labelled region, `#connection` and `#confirm-dialog`.
    - The menu is `templates/ui/nav.html`. `views.navigation` gives `nav_section`, which sets `aria-current`.
    - Messages are callouts (`ui/messages.html`).
  - **Icons:** `{% load ui %}{% icon "name" %}` from `static/icons/sprite.svg`, a Lucide subset (ISC). To add one, copy its `<symbol>` from lucide-static.
  - **A page:** `{% block breadcrumbs %}` with `nav.breadcrumbs` (aria-label "Breadcrumb"), then `header.page-header` (h1, `.page-meta`, `.page-actions`).
    - Problems and results are `.callout-*` (success, warning, error), never bare coloured text.
    - Empty lists get an `.empty-state`: icon, title, a sentence and the main action. The header hides its own copy of that action while the list is empty.
  - **Tables:**
    - List tables are `table.table-stack`, with a `data-label` (the column's name) on every `td`, so phones get cards.
    - A wide table that must scroll gets `.table-wrap` with `tabindex="0" role="region"` and a name.
  - **Forms:**
    - `data-confirm="…"` opens `#confirm-dialog`. Its title and confirm button are the submit button's text, and a `danger` button makes it red.
    - A submitted form shows a busy button and ignores a second submit.
    - `.file-field` gives a file input Persian text.
    - A form that comes back with errors starts with an `.error-summary` marked `data-autofocus`.
  - **Live updates:**
    - Use `hx-swap="morph:outerHTML"`, so focus stays on the button you're on. Give the interactive parts stable ids.
    - The view answers 286 when nothing is active: htmx swaps it in, then stops polling.
    - A failed request shows `#connection`.
    - For HTMX requests, `accounts.middleware.LoginRequired` and `TwoFactorMiddleware` answer an ended session with `HX-Redirect`, so the whole page moves; a plain redirect would swap the login page into the fragment.
  - **Checks:** the browser scan (`tests/web/e2e/test_pages.py`) must stay at zero. `baseline.json` is empty, in light mode at 360–1366 px and in dark mode on desktop.
- **Help (`/help/`, [dashboard/help.py](src/sms_sender_web/dashboard/help.py), plan 05 P6):** one short page in the glossary's words: the five steps, the rules every send keeps, and what each status, delivery status, button, stop and role means.
  - Each item is shown under the label the other pages use (`SUBMISSION_STATUS`, `DELIVERY_STATUS`, `ROLE_LABELS`, the buttons' own msgids), so the help never names a thing twice.
  - `tests/web/test_help.py` fails when a status or a delivery status has no explanation: a new status needs its line here.
  - The rule about the sending window names the default new campaigns get (`SystemSettings.default_send_window`, else 08:00–21:00).
  - The report and the campaign page link to `/help/#statuses` and `#delivery`.
- **Parity with the CLI ([parity.py](src/sms_sender_web/parity.py), plan 05):**
  - Every `sms-sender` command and option, and this app's management commands, has an entry. Each entry is one or more of:
    - `Control(page, role)`;
    - `Implied(how)`;
    - `Excluded(why)` (Saeed approved these on 2026-10-04);
    - `Planned(phase, what)`.
  - Lookup: the exact key, then the same option of `send` for `ALIASES` (retry-failed, dry-run, preview), then `* --option`.
  - A control carries `data-cli="<command> <option>"` (comma-separated for several).
  - `tests/web/test_parity.py` fails when:
    - a CLI command or option has no entry;
    - an entry names something the CLI doesn't have;
    - a control doesn't render on its page for its role, or does for the role below.
  - Add a CLI option → add its entry and its control.
  - P6 allows no `Planned` left.
- **Browser tests (`tests/web/e2e/`, marker `e2e`, opt-in):**
  - Playwright drives the installed Google Chrome (`channel="chrome"`) against pytest-django's live server, in sandbox mode with the real worker in a thread (`sandbox_worker`).
  - The journey finds everything by its Persian name: `fa("msgid")` is the catalog's text. A redesign that keeps the words keeps the test.
  - `test_pages.py` opens every page in `tests/web/world.py`'s `PAGES` at 360 / 390 / 768 / 1024 / 1366 px. It checks for horizontal overflow, runs axe-core (vendored for tests only, MPL-2.0), and finds controls smaller than 24 × 24 px (`small_targets`, WCAG 2.2 2.5.8; inline links and checkboxes inside their label are exempt).
  - `test_keyboard.py`: signing in with the keyboard alone, the skip link, and a visible focus ring on everything Tab reaches.
  - `test_operations.py`: pause and resume; a stopped send resumed; a send the window paused going on by itself (the window is flipped open in the campaign's settings, as if the clock reached it); an unknown outcome reconciled from the sandbox outbox, then the report's "did anyone get it twice?" panel; a requeue's confirmation; the report's filters, an audience and a download; a test notification; an admin's password reset, backup and check, and purge; adopting a CLI campaign.
  - `test_journey.py` also checks the polite live region (`#live-status`), which is what a screen reader announces when the stage changes.
  - Known problems live in `baseline.json`, a ratchet: a new problem fails, and a fixed one still listed fails too.
  - `world.py` builds the made-up dashboard (users, segments, a campaign in every state) for these tests and the parity test.
- **Packaging:** the image installs the package, not the source tree, so each app's `templates/` must be listed in `[tool.setuptools.package-data]`.
- **Docker:** `Dockerfile` + `compose.yaml` (service `web`, gunicorn with threaded workers (`gthread`: sync workers stall on browsers' idle connections and answer "Internal Server Error" when killed), `/healthz`; service `worker`, health check `manage.py worker_status`). The port is published on `${BIND_ADDR:-127.0.0.1}:${WEB_PORT:-8000}`, so it's shared over NetBird only on purpose. Images are `sms-sender-dashboard:${IMAGE_TAG:-latest}`. Both services' logs are capped (json-file, 10 MB × 5), since they hold phone numbers. `DJANGO_TRUST_PROXY_SSL=1` sets `SECURE_PROXY_SSL_HEADER`, only for a TLS proxy that overwrites `X-Forwarded-Proto`. The DevOps handover is [docs/deploy.md](docs/deploy.md); keep it in step with these files.
- **Backups ([backup.py](src/sms_sender_web/backup.py), no Django):** `manage.py backup` / `verify_backup` / `restore_backup` (in `jobs/management/commands/`, with `worker_status`).
  - A backup copies `app.db`, `db/*.db` and `segments/*` into `BACKUP_DIR/<UTC stamp>/` (`SMS_SENDER_BACKUP_DIR`, default `<data>/backups`). Not the sandbox or exports. The dashboard's own backup, taken before an admin changes a sent message, uses `keep=None`, so it never prunes the scheduled ones.
  - DBs go through SQLite's online backup (a plain copy misses the `-wal`). Each is switched to DELETE mode, its leftover `-shm` removed, and integrity-checked.
  - `manifest.json` holds sizes, SHA-256 and row counts.
  - The backup is written to `.partial` and renamed when complete. Pruning (keep N) happens only after a good backup.
  - Restore verifies first and puts back missing files. It replaces an existing file only with `--replace <path>`, moving it and its `-wal` / `-shm` aside (`.before-restore-<stamp>`). It takes each campaign DB's run lock.
  - A campaign DB restored from before a send doesn't know about that send. Never make restore overwrite by default.

## Conventions

- **Recipient phone lists and state DBs are PII.** Keep state DBs in `data/db/` and segment / user-id lists in `data/segments/`; `data/` is gitignored as a whole, and so are root-level `*.csv` / `*.txt` / `*.xls*` (which catches `export-failed`'s default `./failed.csv`) and `*-numbers.*` / `*_numbers.*` anywhere. Never commit one — stage files by name, not with `git add -A` / `git add .`. When moving a DB, update every `state = …` in `sms-sender.toml` too: a path that no longer exists silently opens a fresh, empty DB, and that run re-sends to everyone already sent.
- Logs go to `logs/sms-sender.log` (rotating, 5 MB × 5) and are formatted as `key=value` pairs by `KeyValueFormatter`. Pass structured fields via `logger.info("event_name", extra={...})`, not f-strings, so they stay greppable. An `extra` key must not be a `LogRecord` attribute (`created`, `name`, `msg`, `args`, `module`, …): logging raises `KeyError` mid-run. `tests/test_logging_fields.py` checks every call.
- Adding a new Kavenegar status code: extend the relevant frozenset in `classifier.py`. Don't add per-code branching elsewhere.
