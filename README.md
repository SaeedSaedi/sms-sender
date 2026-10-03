# sms-sender

[![Release](https://img.shields.io/github/v/release/SaeedSaedi/sms-sender)](https://github.com/SaeedSaedi/sms-sender/releases)
[![Python](https://img.shields.io/badge/python-3.10%2B-blue)](https://www.python.org/)
[![License](https://img.shields.io/github/license/SaeedSaedi/sms-sender)](LICENSE)

Reliable bulk SMS sender for the **Kavenegar `verify/lookup`** endpoint.

- **No double sends.** A SQLite state DB tracks every recipient — re-running
  the same input only sends to numbers that haven't been confirmed yet. Only
  one `sms-sender` process can use a state DB at a time; a second one exits.
- **Resumable.** A crash mid-run is safe: on restart, orphan in-flight rows
  are reclaimed and pending rows continue.
- **Retries.** Network timeouts and transient `409` server errors back off
  and retry; per-recipient errors (bad template, invalid receptor) are marked
  permanent and skipped on resume.
- **Progress + logs.** A `tqdm` bar shows sent / failed / left, and every
  important event is logged structured to a rotating log file.

## Install

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
```

## Configure

```bash
cp .env.example .env
# edit .env and set KAVENEGAR_API_KEY
```

## Input format

Either a `.txt` (one number per line) or a `.csv` (the **first non-empty
cell** of each row is the phone). Iranian formats are all accepted and
normalized to `09XXXXXXXXX`:

```
09123456789
+989120000000
0098 912 000 0000
9123456789
# comments and blank lines are ignored
```

Persian (`۰۱۲۳…`) and Arabic-Indic (`٠١٢…`) digits are converted automatically.
A first row with no digits at all (e.g. an Excel `Phone Number` header) is
skipped as a column header instead of being counted as an invalid number.

## Usage

```bash
sms-sender send \
  --input ./numbers.csv \
  --template my-verify-template \
  --token 12345 \
  --workers 5 \
  --state ./sms_state.db \
  --log-file ./logs/sms-sender.log
```

Re-run the same command after a crash, network outage, or credit top-up — it
picks up exactly where it left off.

### Per-recipient tokens

When each recipient needs their own values (name, coin, …), take the tokens
from CSV columns instead of passing them statically. The CSV needs a header
row, and its first column is the phone:

```csv
phone_number,first_name,last_token,trade_side
09123456789,علی,ترون,Buy
```

```bash
sms-sender send --input trades.csv --template transaction-1 \
  --token-column token=trade_side \
  --token-column token10=first_name \
  --token-column token20=last_token \
  --value-map trade_side:Buy=خرید \
  --value-map trade_side:Sell=فروش
```

`--value-map COLUMN:FROM=TO` translates a column's values before sending.
Rows that can't be sent as-is — an empty cell, a value with no `--value-map`
entry, or more spaces than the token allows — are recorded as invalid
(see `export-failed`) rather than sent. `dry-run` and `preview` accept the same
flags, so you can check every row and the exact POST bodies first. In a
profile, write the flags as lists:
`token_column = ["token=trade_side", "token10=first_name"]`.

### Safer first runs

Two flags catch the common mistakes (wrong template, expired credit) **before**
fanning out across 10k rows:

```bash
sms-sender send --input numbers.csv --template my-tpl --token 12345 \
    --smoke-test            # send to the first phone synchronously, abort if it fails
# Add --no-preflight to skip the account/info check (default is on).
```

You can also dry-run the exact request without sending:

```bash
sms-sender preview --phone 09123456789 --template my-tpl --token 12345
sms-sender preview --input numbers.csv --template my-tpl --token 12345 --limit 5
sms-sender preview --phone 09123456789 --template my-tpl --token 12345 --send
```

`preview` prints the URL and POST body for each phone. With `--send` and a single
`--phone`, it actually sends (and does **not** touch the state DB).

### Retrying failures

After fixing a template/token issue, promote retriable failures back to
pending and re-send in one shot:

```bash
sms-sender retry-failed --input numbers.csv --template my-tpl --token 12345
# Add --include-permanent to also reset failed_permanent rows.
```

### Throughput control

`--workers` controls parallelism; `--rate` caps total requests per second on
top of that. With 20 workers and `--rate 10/s`, half the workers will be
parked on the rate gate at any moment.

```bash
sms-sender send --input numbers.csv --template my-tpl --token 12345 \
    --workers 20 --rate 10/s     # also: 60/m, 3600/h, "5" (== 5/s), "0" (off)
```

### End-of-run notifications

Get pinged on Slack/Telegram (or any webhook) when a long run finishes:

```bash
sms-sender send … --notify slack:https://hooks.slack.com/services/AAA/BBB/CCC
sms-sender send … --notify telegram:<bot_token>:<chat_id>
sms-sender send … --notify https://example.com/webhook   # generic JSON POST
```

The Slack and Telegram forms post a human-readable report (counts, top
errors, sends/sec). The generic `https://` form POSTs the structured run
summary as JSON. Failures are best-effort: a notification that doesn't go
through never aborts the run, only logs a warning.

### Profiles (`sms-sender.toml`)

Stop typing `--template … --token … --workers …` every run. Drop a
`sms-sender.toml` next to your input file:

```toml
[profile.default]
workers = 10
rate = "10/s"
log_file = "./logs/sms-sender.log"

[profile.verify]
template = "my-verify-template"
token = "12345"

[profile.welcome]
template = "welcome"
token = "salam"
```

Then:

```bash
sms-sender send --input numbers.csv                    # uses [profile.default]
sms-sender --profile welcome send --input list.csv     # default + welcome merged
sms-sender --config /path/to/other.toml send --input … # explicit config file
```

Resolution order (highest wins): explicit CLI flag → named profile →
`[profile.default]` → built-in default. Any flag the CLI knows about can be
set in the profile (`template`, `token`, `token2`, `workers`, `rate`,
`max_attempts`, `timeout`, `state` (db_path), `log_file`, `smoke_test`,
`no_preflight`, `verbose`/`quiet`, etc.).

### Other commands

```bash
sms-sender status --state ./sms_state.db          # row counts by status
sms-sender export-failed --state ./sms_state.db   # dump permanent failures to CSV
sms-sender dry-run --input ./numbers.csv          # parse + normalize only, no API
```

## Exit codes

| Code | Meaning |
|---|---|
| 0 | All recipients sent successfully. |
| 1 | Run finished but some rows failed (permanent or retriable). |
| 2 | Run halted on an account-level error (no credit, bad API key, plan), or another `sms-sender` is already using the same state DB. Fix and re-run. |

## Architecture

```
CLI ──► InputLoader ──► StateStore (SQLite, WAL, immediate commit)
                            │
                            ▼
                   Runner (ThreadPoolExecutor)
                     ├─ Sender (Kavenegar SDK + tenacity retry)
                     ├─ Classifier (status code → retry/permanent/halt)
                     ├─ tqdm progress bar
                     └─ Structured logger (console + rotating file)
```

State machine per recipient:

```
            upsert
   (input) ────────► pending ──claim──► in_flight ──ok──► sent
                       ▲                      │
                       │                      ├──permanent──► failed_permanent
                       └────retriable─────────┴──halt/retries-exhausted──► failed_retriable
```

Re-runs reclaim `in_flight` (orphaned by crash) and `failed_retriable` rows.
`sent` and `failed_permanent` rows are never touched again unless the DB
is deleted.

## Testing

```bash
pytest
```
