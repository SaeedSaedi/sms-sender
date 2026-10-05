# sms-sender

[![Tests](https://github.com/SaeedSaedi/sms-sender/actions/workflows/tests.yml/badge.svg)](https://github.com/SaeedSaedi/sms-sender/actions/workflows/tests.yml)
[![Release](https://img.shields.io/github/v/release/SaeedSaedi/sms-sender)](https://github.com/SaeedSaedi/sms-sender/releases)
[![Python](https://img.shields.io/badge/python-3.10%2B-blue)](https://www.python.org/)
[![License](https://img.shields.io/github/license/SaeedSaedi/sms-sender)](LICENSE)

Reliable bulk SMS sender for the **Kavenegar `verify/lookup`** endpoint.

- **No double sends.** A SQLite state DB tracks every recipient — re-running
  the same input only sends to numbers that haven't been confirmed yet. Only
  one `sms-sender` process can use a state DB at a time; a second one exits.
- **Resumable.** A crash mid-run is safe: on restart, rows that were mid-send
  are marked `unknown` (they may already have the SMS) instead of being sent
  again, and pending rows continue.
- **Retries.** Requests that never reached Kavenegar (connection refused, DNS,
  connect timeout) and its "try later" codes (`409`, `451`) back off and
  retry. A timeout *after* the request went out is never retried — Kavenegar
  may have sent it — so the row becomes `unknown`. Per-recipient errors (bad
  template, invalid receptor) are marked permanent and skipped on resume.
  Every call to Kavenegar is recorded in the state DB's `attempts` table.
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
# edit .env and set KAVENEGAR_API_KEY (and SHLINK_API_KEY for short links)
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

### One campaign, one state DB

Give each campaign a name and it gets its own state DB, `data/db/<name>.db`.
There's nothing to purge between campaigns, and each one keeps its history:

```bash
sms-sender send --campaign coin-price-7 --input data/segments/seg2.csv --template coin-price ...
sms-sender status --campaign coin-price-7   # template, last run, counts
```

The DB remembers what the campaign sends (template, static tokens, token
columns). A later run with different settings is refused once anything may
have gone out, so one campaign never mixes two message versions; before that,
fixing a wrong template is fine. `--allow-settings-change` overrides it.
`--state` (or `state` in a profile) still picks a DB explicitly and wins over
`--campaign`; in a profile, write `campaign = "coin-price-7"`.

### Opt-out list

People who must never get a campaign go in one or more lists (same formats as
`--input`):

```bash
sms-sender send --campaign coin-price-7 --input ... --opt-out data/opt-out.txt
```

Matching recipients become `suppressed` and are never sent; anyone who
already got the campaign stays `sent`. In a profile:
`opt_out = ["data/opt-out.txt"]`.

### Sending hours

Campaign SMS only go out between **08:00 and 21:00 Tehran time** by default.
Outside that window a run refuses to start (exit code 2); if the window closes
mid-run, it stops claiming new recipients, says so, and the same command
continues the next day.

```bash
sms-sender send ... --send-window 09:00-20:00   # a different window
sms-sender send ... --send-window off           # any time
```

`SMS_SENDER_SEND_WINDOW` (env or `.env`) or `send_window` in a profile set it
too.

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
entry, or a value Kavenegar would reject (more spaces than the token allows,
an underscore, a line break, more than 100 characters) — are recorded as
invalid (see `export-failed`) rather than sent. Static `--token…` values are
checked by the same rules. `dry-run` and `preview` accept the same
flags, so you can check every row and the exact POST bodies first. In a
profile, write the flags as lists:
`token_column = ["token=trade_side", "token10=first_name"]`.

### Safer first runs

Two flags catch the common mistakes (wrong template, expired credit) **before**
fanning out across 10k rows:

```bash
sms-sender send --input numbers.csv --template my-tpl --token 12345 \
    --smoke-test            # send to the first phone synchronously, abort if it fails
# Add --no-preflight to skip the account checks (default is on).
```

Before sending, the account checks also read (never change) the Kavenegar
account settings: a run on an account in **debug mode** stops, because nothing
would be delivered. With `--approval-test`, the test SMS's real cost gives an
**estimate** — cost × recipients — and a run the remaining credit can't cover
stops before the fan-out. The end-of-run report shows what the run cost.

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

### Unknown outcomes

If a request may have reached Kavenegar without a clear answer (a read
timeout, a dropped connection, or the process dying mid-send), the row
becomes `unknown` and is **never resent automatically** — the recipient may
already have the SMS. Every run checks old-enough `unknown` rows with
Kavenegar first; you can also check them yourself:

```bash
sms-sender reconcile --state ./sms_state.db
```

It asks Kavenegar which messages went to each phone (`sms/statusbyreceptor`)
and never sends anything. Kavenegar answers per calendar day: it lists that
day's messages to the phone, and messages from any campaign DB in the same
folder don't count.

- one message found → `sent`
- several found → `needs_review`; decide yourself, and only if you're sure
  they didn't get it: `sms-sender reset --status needs_review`
- none found, for an attempt under a day old → it never went out:
  `failed_retriable`, and the next `send` delivers it
- none found, for an older attempt → `needs_review`, because Kavenegar
  doesn't list old days reliably

`--review-not-found` sends every "none found" to review instead.

Rows less than 5 minutes old wait (`--min-age`).

If the process stopped right after Kavenegar accepted an SMS, before the
row was marked, the recorded call settles it as `sent`, with no lookup.

To check that nobody got a campaign twice:

```bash
sms-sender check-sends --campaign coin-price-7
```

It counts each phone's SMS from the record of every call to Kavenegar,
with approval tests counted apart. It never sends anything. Exit 1 if a
phone got it twice (`TWICE`) or may have (`MAYBE`).

### Delivery reports

`sent` means Kavenegar accepted the SMS. Whether it reached the phone comes
later:

```bash
sms-sender delivery --campaign coin-price-7   # ask Kavenegar, store the answers
sms-sender status --campaign coin-price-7     # delivery: delivered 10,234 · undelivered 120 · …
```

Kavenegar only reports delivery for **48 hours** after sending, so run
`delivery` a few times inside that window (e.g. after 10 minutes, an hour, a
day). It never sends anything and is safe to run during a send.

### Short links

Give a campaign a destination and the token that carries the link, and
every recipient gets their own short link, `https://kifpool.me/u/<code>`.
The link goes in the template token you name:

```bash
sms-sender send --campaign coin-price-7 --input data/segments/vip-2.csv \
    --template coin-price --link-url https://kifpool.me/offer --link-token token3
```

All the links are made at Shlink **before the first SMS**. If even one
can't be made, nothing is sent and the run exits with code 2. Re-running the
same command retries the missing links and reuses the ones that exist, so
no recipient ever gets two different links.

If the link settings change before someone is sent (say the destination
was wrong and you declined the approval test), they get a new link with
the new settings; the old one is never used. Recipients who were already
sent keep the link they got. Two more checks run before sending:
- A link that would expire within a day of its SMS gets more time first.
- With `--approval-test`, the test SMS gets its own link, so your click
  doesn't count for a recipient.

- **What's in a link:** the destination plus `utm_source=sms`,
  `utm_medium=sms`, `utm_campaign=<campaign>`, `utm_content=<segment>` and
  `r`, a random reference.
  - Change the UTM values with `--utm-*`.
  - No phone number or user ID ever appears in a link or reaches Shlink.
  - `r` maps back to the recipient only in the campaign's DB (see
    `export-attribution`).
- **Template formats:**
  - `--link-format url` (the default) puts the whole link in the token.
  - `--link-format code` puts only `<code>`, for templates whose text already
    contains `https://kifpool.me/u/`.
  - A pattern with `{code}` fits a template whose text holds part of the
    URL. E.g. `--link-format 'u/{code}'` for a template like
    `introducecoin-c`, whose text has `https://kifpool.me/` before the token.
- **Strategies:**
  - `--link-strategy recipient`: the default, one link per person.
  - `segment`: one link per segment.
  - `campaign`: one link for everyone.
  - Only personal links tell you who clicked.
- **Expiry:** links work for 7 days (`--link-expiry-days`).
- **Creation speed:** links are made at up to 10 per second by default,
  until Shlink's real speed is measured; `--link-rate` changes it.
- **Allowed destinations:** https on kifpool.me or its subdomains by default.
  `SMS_SENDER_LINK_DOMAINS=kifpool.me,example.org` allows more.
- **Checking first:** `dry-run --campaign … --link-url … --link-token …`
  prints the long URL, title and tags a link would get, without calling
  Shlink. `preview --link-token token3` shows where the link goes in the
  request.

Shlink's key goes in `.env` as `SHLINK_API_KEY`; `SHLINK_BASE_URL` is
`https://kifpool.me/u` unless set.

### User IDs and segments

Map a CSV column to each recipient's user ID, and record which segment
the file is:

```bash
sms-sender send --campaign coin-price-7 --input data/segments/vip-2.csv \
    --template coin-price --user-id-column user_id --segment vip-2 …
```

- **Blank user ID:** the recipient is still sent, and is reported as
  **missing user ID**.
- **Two different user IDs for one phone:** that phone can't be attributed,
  so every row with it is invalid and it isn't sent. This applies within one
  file, and against an earlier import into the same campaign.
  - The phone gets the status `invalid`, which no retry undoes.
  - After fixing the IDs at the source, `reset --status invalid` puts it back
    in the queue (it asks first).
- **Segment:** defaults to the file's name (`vip-2.csv` → `vip-2`). Reports
  and links use it.

`dry-run --user-id-column user_id` shows both counts before anything is sent.

### Clicks and exports

```bash
sms-sender clicks --campaign coin-price-7              # fetch counts from Shlink, per segment
sms-sender export-attribution --campaign coin-price-7  # ref → user ID, for the backend
sms-sender export-clickers --campaign coin-price-7     # who clicked, with phone numbers
```

`clicks` stores each link's visit count. Bots and link-preview fetchers are
excluded. It shows, per segment, how many were sent, the clicks, and how
many recipients clicked their own link; recipients without a user ID are
counted separately. Shared segment and campaign links count for the segment
or the campaign, never for a person. It never sends anything.

The exports write to `data/exports/` (gitignored) unless `--out` is given:
- **`export-attribution`** has no phone numbers. Each row has `ref`, the
  user ID (or `missing user ID`), segment, link, accepted time (ISO 8601,
  UTC), delivery status and clicks. That's what the backend needs to join
  `r` from its own logs to users.
- **`export-clickers`** contains phone numbers. Treat it like the
  recipient lists.

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

## Dashboard (Phase 3, in progress)

A Persian, right-to-left web dashboard for running campaigns. It's an
internal, multi-user tool built with Django and HTMX. So far it has:
- sign-in, with two-step verification for operators and admins;
- the three roles, and a users page for admins;
- an activity log;
- segments: upload a CSV or TXT list (Excel's encodings and separators are
  fine), choose the phone, user-ID and token columns, and see the counts:
  valid, invalid, repeated, missing or conflicting user IDs, and numbers on
  the suppression list;
- the suppression list: operators add numbers (pasted or from a file),
  admins remove them, and every send leaves them out;
- campaigns, run from the browser in the spec's stages:
  1. **Settings:** the segment, the template, and what fills each token: a
     fixed value, a column of the segment, or the short link. Then value
     translations, the link's destination, the sending window and a rate
     limit.
  2. **Check and preview:** runs whenever you open the campaign, and sends
     nothing: who would get the SMS, who is suppressed or already sent, and
     what's in the way. Then each recipient's message (the first 5, 10 or
     20, or one number you look up) and what the short-link service will be
     asked for.
  3. **Test SMS:** runs the checks, checks the account, makes the test
     SMS's own short link, and sends one SMS to *your own* number (set on
     «حساب من» / My account). It shows the cost per SMS, the estimate and
     the credit. You approve it or reject it. The recipients' links are made
     when sending starts, before any SMS, so the test doesn't wait for them.
  4. **Send:** only after an approved test SMS for the current settings.
     Start now, or schedule it for a Solar Hijri date and a Tehran time (up
     to 30 days ahead). A scheduled send can start at once, or be taken back
     without cancelling anyone. Optionally it goes to one recipient first
     and stops if that SMS doesn't go out (the CLI's `--smoke-test`).
     Progress updates live, with pause, resume and cancel.
  5. **Follow-up:** reconcile, update delivery statuses and clicks.
     None of these sends an SMS.

  Changing the message (template, tokens, link, segment) needs a new test
  SMS. While a send is scheduled, the settings wait: cancel the schedule to
  change them. Once sending has started, a campaign's settings are fixed.

  A campaign always shows its stage and the next step. When a send ends, its
  results and what's left (unknown, not sent, rejected) sit together, each
  with its action. A send the sending window stopped goes on by itself
  when the window opens.
- a template library («قالب‌ها»): a copy of each Kavenegar template's text.
  The settings page then previews the message with the first recipient's
  values, its length in SMS parts, and any token the text uses but nothing
  fills. It also warns when Persian text would show left to right on
  phones, because its first word is Latin.
- the campaign list: the CLI's campaigns and the dashboard's;
- a report per campaign:
  - submission and delivery statuses, cost, and clicks per segment (bots and
    link previews left out);
  - every recipient, numbers masked. An operator can reveal one number at a
    time, and each reveal is recorded;
  - downloads: a summary for everyone; attribution (no phone numbers) and the
    people who clicked (with phone numbers) for operators. Each download is
    recorded;
- a status page («وضعیت سرویس‌ها»): Kavenegar credit and account settings,
  the short-link service, and whether the worker is running.

It works on phones (the menu moves into a drawer, tables become cards), by
keyboard and with screen readers. It follows the system's dark mode.

The campaign pages hand their work to the worker (`docker compose up`
starts it next to the web app), and the pages follow it as it runs.

### Sandbox: try everything without sending

Set `SMS_SENDER_SANDBOX=1` in `.env`, then run `docker compose up -d`. Both
services read `.env`. In sandbox mode:
- Kavenegar and the short-link service are simulated, and no request leaves
  the machine.
- Every page shows a banner: «محیط شبیه‌سازی».
- Everything lives in `data/sandbox/`, the users included, so create a
  superuser there too:
  `docker compose exec web python manage.py createsuperuser`.
  Simulated sends never mix with real campaigns.
- To see every state: numbers ending in `000` are rejected, and numbers
  ending in `999` are accepted but their reply is lost. Their status becomes
  unknown until reconciliation finds them, without sending again. Every SMS
  costs 3,020 rials, and links get a few clicks.
- Every simulated SMS is written, with its final tokens, to
  `data/sandbox/sandbox-outbox.jsonl`. The status page lists the latest ones,
  so you can read exactly what would have gone out.

Remove the line (or set it to `0`) and restart to go back to real sending.

Uploaded lists are kept in `data/segments/`, which git ignores, like every
other list of phone numbers. Pages show numbers masked (`۰۹۱۲*****۳۴`).

### Hosting it

[docs/deploy.md](docs/deploy.md) is the handover for whoever runs it:
configuration, TLS and NetBird access, health checks, backups
(`python manage.py backup`, `verify_backup`, `restore_backup`), upgrades and
rollback, and a runbook.

It also has a background **worker** (`python manage.py run_worker`, the
`worker` service in Compose) that runs the jobs the dashboard queues: sends,
reconciliation, and delivery and click updates, one at a time.
- **Pause** stops taking new recipients; requests already in flight finish
  and are recorded.
- **Resume** continues from the campaign DB without sending anyone twice.
- **Cancel** marks everyone still waiting as `cancelled`; SMS already
  accepted are final.
- **Restarts:** if the worker stops or dies, its job is picked up again.
- **Scheduled updates:** while idle, it queues delivery updates during
  Kavenegar's 48-hour window, and click updates.
- **Scheduled sends** wait in the queue until their time.

Run it locally with Docker:

```bash
cp .env.example .env              # set DJANGO_SECRET_KEY (and the other keys)
docker compose up --build
docker compose exec web python manage.py createsuperuser
```

Then open http://127.0.0.1:8000 and sign in as that superuser. Admins and
operators confirm a code from an authenticator app (Google Authenticator,
Microsoft Authenticator, …) at every sign-in. The first sign-in shows a QR
code to set the app up. Then create everyone else on the users page
(«کاربران»). Each person has their own account and exactly one role:

| Role | Persian | Can |
|---|---|---|
| `viewer` | مشاهده‌گر | see campaigns and reports (phone numbers masked); no code needed |
| `operator` | اپراتور | also create and run campaigns, and see full numbers one at a time |
| `admin` | مدیر سامانه | also manage users, the suppression list and settings, and read the activity log |

The campaign actions arrive in the next steps; the roles already decide who
gets them. A user without a role can sign in but sees nothing until an
admin gives them one. Superusers are always admins.

Someone who loses their phone asks an admin to reset their two-step
verification on the users page; they set the app up again at their next
sign-in. If the only admin loses theirs:
1. Run `docker compose exec web python manage.py createsuperuser` to create a
   second admin.
2. Sign in with it and reset the first admin's verification.
3. Deactivate the spare account.

Sign-ins, failed sign-ins, codes and every change to an account are
recorded in the activity log («سابقه فعالیت‌ها»). The username tried is
recorded, never a password.

If PyPI is only reachable through a local proxy (e.g. privoxy on port 8118),
pass it to the build. Docker Desktop forwards `host.docker.internal` to
services on your Mac:

```bash
docker compose build --build-arg HTTPS_PROXY=http://host.docker.internal:8118 \
                     --build-arg HTTP_PROXY=http://host.docker.internal:8118
```

To let colleagues on NetBird reach it:
1. Set `BIND_ADDR` to this machine's NetBird IP.
2. Add that IP to `DJANGO_ALLOWED_HOSTS` and `DJANGO_CSRF_TRUSTED_ORIGINS`.

NetBird's access policies decide who can reach the port, and every page
still needs a login.

Without Docker, run `pip install -e ".[web]"`, then `python manage.py
migrate`, `python manage.py createsuperuser` and `python manage.py
runserver`.

All of the dashboard's Persian text lives in
`src/sms_sender_web/locale/fa/LC_MESSAGES/django.po`. After editing it, run
`msgfmt -o django.mo django.po` in that folder; a test checks that the two
match. A Persian-speaking specialist reviews this file before the dashboard
ships.

## Exit codes

| Code | Meaning |
|---|---|
| 0 | All recipients sent successfully. |
| 1 | Some rows failed (permanent or retriable) or are `unknown` / `needs_review`; or the run stopped early (Ctrl-C, or the sending window closed) and the same command continues it. |
| 2 | Nothing more was sent, because of one of these. Fix it and re-run:<br>• an account-level error (no credit, bad API key, plan);<br>• outside the sending window;<br>• a short link couldn't be made, or Shlink refused the key;<br>• another `sms-sender` is already using the same state DB;<br>• the DB belongs to another campaign, or was sent with other settings. |

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
(input) ─► pending ─claim─► in_flight ─┬─ accepted ───────────► sent
                                       ├─ rejected ───────────► failed_permanent
                                       ├─ not sent ───────────► failed_retriable
                                       └─ may have been sent ─► unknown
```

| Status | Meaning | Sent again? |
|---|---|---|
| `pending` | not sent yet | yes, by the next `send` |
| `in_flight` | being sent right now | — one left behind by a crash becomes `unknown` |
| `sent` | Kavenegar accepted it | never |
| `failed_retriable` | definitely not sent: no credit, the request never reached Kavenegar, or retries ran out | yes, by the next `send` |
| `failed_permanent` | Kavenegar rejected it: bad template, invalid number | only after `retry-failed --include-permanent` or `reset` |
| `invalid` | the input says not to send it: the phone came with two different user IDs | only after `reset --status invalid` (with a confirmation), once the IDs are fixed |
| `unknown` | may have been sent: a timeout after the request, a crash mid-send | never automatically; `reconcile` asks Kavenegar |
| `needs_review` | `reconcile` couldn't decide | only after `reset --status needs_review` |
| `suppressed` | on the opt-out list | only after `reset --status suppressed`, and only once it's off the list |

Only `pending` and `failed_retriable` rows are ever claimed, so a `send` can
never reach a row that has, or may have, the SMS.

## Testing

```bash
pytest                                  # everything but the browser tests
pip install -e ".[dev,web,e2e]"         # once, for the browser tests
pytest -m e2e                           # the dashboard in Google Chrome
E2E_SHOTS=/tmp/shots pytest -m e2e      # also save a screenshot of every page and width
```

The browser tests drive the Google Chrome you have installed, through
Playwright. They run the dashboard in sandbox mode with a real worker, so
nothing is sent:
- a whole operator journey, from the first sign-in to the report;
- every page at 360, 390, 768 and 1366 px: no sideways scrolling, and no
  problem axe-core can find.

Problems known before the redesign are listed in
`tests/web/e2e/baseline.json` until they're fixed.

GitHub Actions runs the suite on Python 3.10 and 3.14, and the browser tests,
for every push to `main` and every pull request (`.github/workflows/tests.yml`).
