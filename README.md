# sms-sender

Reliable bulk SMS sender for the **Kavenegar `verify/lookup`** endpoint.

- **No double sends.** A SQLite state DB tracks every recipient — re-running
  the same input only sends to numbers that haven't been confirmed yet.
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
| 2 | Run halted on an account-level error (no credit, bad API key, plan). Fix and re-run. |

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
