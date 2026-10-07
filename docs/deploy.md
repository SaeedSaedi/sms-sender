# Deploying the campaign dashboard

For whoever hosts and operates the dashboard. It covers what to run, how to
configure and back it up, how to upgrade it, and what to do when something
goes wrong. How campaigns themselves work is in the [README](../README.md).

## What runs

One Docker image, `sms-sender-dashboard`, runs as two services from
[`compose.yaml`](../compose.yaml):

| Service | Command | Job |
|---|---|---|
| `web` | `migrate`, then gunicorn on port 8000 (threaded workers) | The Persian dashboard. Applies database migrations on every start. Its workers are threaded (`gthread`) because browsers reach it directly: a sync worker would be held by a browser's idle connection until killed. |
| `worker` | `python manage.py run_worker` | Runs what the dashboard queues in two lanes. Sends go one at a time. Test SMS, reconciliation, and delivery and click updates run beside them, so an urgent test never waits behind a long send. |

Both share one volume, `/app/data` (`./data` on the host). Everything with
state lives there. The containers run as uid 1000 (`app`).

Until the move, the same app ran natively on a Mac (`./sms-dashboard`,
[README](../README.md)). The Mac launcher and these containers never share a
data folder: it refuses to start while a container has the folder mounted,
and [Moving from the Mac](#moving-from-the-mac) retires the Mac's copy for
good.

**Run exactly one worker.** The design assumes one. A second would run jobs
side by side with the first, and a job that finds its campaign DB busy
fails. (The lock on each campaign DB still keeps two processes from
sending the same campaign.) Don't scale `worker`. A worker that starts while
another one is alive on the same data waits up to two minutes for it, then
exits with code 2 (Compose restarts it). One that stopped cleanly signs off,
so an upgrade's new worker starts at once.

## Build

```bash
docker compose build                      # tags sms-sender-dashboard:latest
IMAGE_TAG=$(git rev-parse --short HEAD) docker compose build   # tag it, for rollbacks
```

The build installs exactly the versions in `requirements/server.lock`, each
checked against its hash, so two builds of one commit are the same. Updating
them is a change of its own (`requirements/server.in` says how), with the
tests and a sandbox rehearsal. The packages come from PyPI; behind a proxy,
pass it in:
`--build-arg HTTPS_PROXY=http://host.docker.internal:8118 --build-arg HTTP_PROXY=…`.

The image holds no secrets, recipient data or local state (`.dockerignore`).

## Configuration

Everything comes from environment variables. Compose reads them from `.env`
([`.env.example`](../.env.example) lists them). Both services need the same
values.

| Variable | Required | Meaning |
|---|---|---|
| `DJANGO_SECRET_KEY` | yes | Long random value: `python -c "import secrets; print(secrets.token_urlsafe(50))"`. Changing it signs everyone out. |
| `DJANGO_ALLOWED_HOSTS` | yes | Host names or IPs people type in the browser, comma-separated. |
| `DJANGO_CSRF_TRUSTED_ORIGINS` | with a host other than localhost | Full origins, e.g. `https://sms.example.internal`. |
| `KAVENEGAR_API_KEY` | yes | The campaign Kavenegar account. It's separate from the account that sends login codes, so a campaign can't use up their credit. |
| `SHLINK_API_KEY` | for short links | A production key of its own, never the development one. Shlink can limit a key to the links it created (role `AUTHORED_SHORT_URLS`). Check that `sms-sender clicks` still counts with such a key before relying on it. |
| `SHLINK_BASE_URL` | no | Default `https://kifpool.me/u`. |
| `SMS_SENDER_LINK_DOMAINS` | no | Where links may lead. Default `kifpool.me` (subdomains included). |
| `DJANGO_SECURE_COOKIES` | behind TLS | `1`: cookies only over HTTPS. |
| `DJANGO_TRUST_PROXY_SSL` | behind a TLS proxy | `1`: trust `X-Forwarded-Proto` from the proxy. Set it only if the proxy overwrites that header. |
| `SMS_SENDER_BACKUP_DIR` | no | Where `manage.py backup` writes. Default `/app/data/backups`. |
| `IMAGE_TAG` | no | Image tag Compose runs. Default `latest`. |
| `BIND_ADDR` | no | Host address Compose publishes the dashboard on. Default `127.0.0.1`. |
| `WEB_PORT` | no | Host port for it. Default `8000`. |
| `HTTPS_PROXY`, `HTTP_PROXY`, `NO_PROXY` | no | Outbound proxy to Kavenegar and Shlink, if the host needs one. |
| `SMS_SENDER_SANDBOX` | never in production | `1`: Kavenegar and Shlink are simulated and nothing is sent. For drills, see [Sandbox drills](#sandbox-drills). |
| `SMS_SENDER_ALLOWED_NUMBERS` | no; only on purpose | Restricted sending: SMS go only to these numbers (comma-separated), from the dashboard and the CLI. A value that isn't a phone number stops all sending. Every page shows a banner while it's set. |
| `DJANGO_DEBUG` | never in production | |
| `SMS_SENDER_ENVIRONMENT` | never on the server | `local` is what `./sms-dashboard` sets on a Mac: the «محلی» badge and "keep me signed in on this Mac". Leave it unset here. |
| `SMS_SENDER_SEND_WINDOW` | no | The command line's default sending window (`08:00-21:00` Tehran, `off`). Dashboard campaigns carry their own. |
| `SMS_SENDER_TEST_NUMBER` | no | The command line's default number for `--approval-test`. Dashboard users set theirs on «حساب من». |
| `SMS_SENDER_DATA_DIR`, `DJANGO_STATIC_ROOT` | set by the image | `/app/data` and `/app/static`. Don't change them. |

Secrets (`DJANGO_SECRET_KEY`, both API keys) live only in `.env` or your
secret store. Never put them in the image, the repository or a ticket.

## Data and what's sensitive

```
/app/data/
  app.db              users, roles, two-step secrets, campaigns, jobs, activity log
  db/<campaign>.db    one per campaign: every recipient, what was sent, the call records
  db/<campaign>.db.lock   run lock; harmless to leave
  db/.dashboard-worker.json   the worker's heartbeat, for the CLI (see below)
  segments/           uploaded recipient lists
  exports/            CSV downloads made from the CLI (made again on demand)
  backups/            manage.py backup (see below)
  sandbox/            sandbox mode only
```

All of it is personal data: phone numbers, user IDs, who clicked what. Keep
the volume on encrypted storage and readable only by the service account.
Never copy it to a laptop, a ticket or chat.

The campaign DBs are the memory that stops a second SMS. A deleted or
replaced campaign DB means the next send of that campaign goes to everyone
again. Don't move, rename or delete files in `db/`.

**Phone numbers are kept for a limited time:** 12 months after a
campaign's last send by default. An admin sets it on «تنظیمات سامانه»
(System settings). Once a day the worker:
- replaces every number in that campaign's DB with a placeholder, and
  rewrites the file so no number is left in it. Its counts stay, and it
  never sends again;
- deletes the segment files no campaign has used since, and the files in
  `exports/` and `backups/` older than that;
- masks the numbers in old jobs and in the activity log.

The suppression list is never touched. To see what would go:
`docker compose exec -T web python manage.py remove_old_numbers --dry-run`.
Keep off-site backup copies no longer than the same period, or numbers
outlive it there.

### Where the data may live

Everything above is SQLite, by design: one host, one worker, files the CLI
can also use. That holds as long as these do:

- **A local disk.** Never NFS, SMB or another network filesystem: SQLite's
  locks and its write-ahead log need a local disk, and on a network share
  two writers can corrupt a DB without any error.
- **Not Docker Desktop's file sharing either.** On a Mac or Windows,
  `./data` is shared from the host into Docker's VM.
  - In the 2026-10-06 rehearsal, SQLite there twice lost `app.db`'s
    shared-memory file (`app.db-shm`) under the worker, which then failed
    with "disk I/O error".
  - That never happened when the app runs natively on the Mac, or on a
    volume on the VM's own disk.
  - So on Docker Desktop, put the data in a named volume
    (`- smsdata:/app/data`, with `volumes: {smsdata: {}}`). On a Linux
    server, a directory on its local disk is right, as in `compose.yaml`.
- **One host.** Both services mount the same volume on the same machine.
  A second web server or worker host is the point to move `app.db` to
  PostgreSQL (the campaign DBs stay files, because the CLI and the rule
  against a second SMS are built on them).
- **The CLI runs inside the container**, not on the host:
  `docker compose exec worker sms-sender …`. On a Linux host the two share
  a kernel and their locks meet. On Docker Desktop (a Mac or Windows) the
  containers run in a VM, and locks don't cross into it. The worker leaves
  `db/.dashboard-worker.json` with its kernel's boot ID; a CLI on another
  kernel refuses to change anything while that heartbeat is fresh (two
  minutes), and warns when it only reads.
- `SMS_SENDER_LOG_FILE` sets where the CLI writes its log (default
  `./logs/sms-sender.log`).

## Network access and TLS

The dashboard is internal. Reach it over NetBird only, never from the
public internet.

- Compose publishes the port (`WEB_PORT`, 8000) on `BIND_ADDR`, `127.0.0.1` by default. Set it
  to the host's NetBird IP to share it. Never use `0.0.0.0` on a network you
  don't control.
- `DJANGO_ALLOWED_HOSTS` and `DJANGO_CSRF_TRUSTED_ORIGINS` must name that
  IP or host name.
- Sign-in needs a password, and two-step verification (an authenticator
  app) for operators and admins. NetBird's access policy decides who can
  reach the login page at all.
- TLS: put a reverse proxy in front of gunicorn. Then set
  `DJANGO_SECURE_COOKIES=1`, and `DJANGO_TRUST_PROXY_SSL=1` if the proxy sets
  `X-Forwarded-Proto`. Without TLS, passwords and codes cross NetBird's
  encrypted tunnel, but not encrypted by the app itself.

The containers call out to `api.kavenegar.com` and the Shlink host over HTTPS.

### The attribution API

The company's backend can read attribution over the same address:
`GET /api/v1/campaigns/` and `GET /api/v1/campaigns/<slug>/attribution/?page=N`
(JSON, 1,000 rows a page).
- Each call needs `Authorization: Bearer <token>`.
- An admin issues tokens on «توکن‌های API» (API tokens) in the dashboard. A token is shown once; only its SHA-256 is stored, so it can't be recovered from the database. It can be revoked there.
- The answers never hold a phone number: `r`, user ID, segment, short link, accepted at, delivery and clicks.
- Every call is recorded in the activity log, with the token's name.

Give the backend NetBird access to the dashboard's port like a person's. The API is read only and sends nothing.

## Moving from the Mac

The dashboard ran on a Mac first. Its data comes to the server as one
backup, restored into an empty data folder before the first start. The
accounts, their roles and their authenticator apps come along in `app.db`,
so nobody is created again and nobody links an app again.

**The rule that matters:** after the move, only the server sends. The Mac's
copy holds the same campaign DBs. A campaign sent from both would reach
everyone a second time, because neither copy knows what the other sent.
`retire` fences the Mac's copy off.

On the Mac, in the checkout (best with no send running; one that is
running stops cleanly, and its job continues on the server):

```bash
./sms-dashboard retire
```

It stops the dashboard and removes its start-at-login agent. It then takes
a final backup, verifies it, and prints its folder. From then on that copy
refuses to start (`data/MOVED.json`), and the command line refuses to send
from it (`data/db/.sending-held.json`). Neither file is part of a backup.

Copy that folder to the server, **encrypted on the way** (it holds every
phone number), e.g. with `age`:

```bash
tar -C data/backups -czf - <time> | age -r <server's key> > sms-move.tgz.age   # on the Mac
```

On the server, in the deployment folder, before anything has started:

```bash
mkdir -p data/backups && age -d -i <key> sms-move.tgz.age | tar -C data/backups -xzf -
sudo chown -R 1000:1000 data
docker compose run --rm --no-deps web python manage.py verify_backup /app/data/backups/<time>
docker compose run --rm --no-deps web python manage.py restore_backup /app/data/backups/<time>
docker compose up -d
```

`restore_backup` into an empty folder puts every file back. Then:
- `.env` on the server has its own `DJANGO_SECRET_KEY`. Everyone signs in
  once more; passwords and authenticator apps work as before.
- Set `SMS_SENDER_ALLOWED_NUMBERS` only if the server should start with
  restricted sending, as the Mac may have had.
- Sign in and check the status page (Kavenegar's credit and settings,
  Shlink, the worker), then the control room. A send that was on its way
  continues from its campaign DB; nobody already sent is sent again.
- Logs, exports, `.env` and the Mac's other backups don't travel.

Keep the Mac's retired copy until the server has run for a while, then
delete its `data/` folder: it holds every phone number. To use that copy
again on purpose (never while the server runs those campaigns), remove both
files `retire` wrote.

## First start

```bash
docker compose up -d
docker compose exec web python manage.py createsuperuser
```

For a fresh install, with no data to bring. (Coming from the Mac? See
[Moving from the Mac](#moving-from-the-mac) instead.) Then sign in. An admin
links an authenticator app at first sign-in, then
gives others their roles on the users page:
- viewer: reads reports;
- operator: runs campaigns;
- admin: also manages users and removes numbers from the suppression list.

Each operator sets their own test number on «حساب من» (My account). The
test SMS before every campaign goes there, then to the team's numbers an
admin keeps on «تنظیمات سامانه» (System settings). Once colleagues join, an
admin can ask there for a second person to approve each test SMS.

### Before the first real send

Sign these off with the team. The campaign DBs are the only record of who
already got each SMS: lose them and the next send reaches everyone again.

- [ ] `data/` is on this host's local disk (see [Where the data may live](#where-the-data-may-live)).
- [ ] Every day's backup leaves the machine, encrypted (see [Backups](#backups)).
- [ ] A restore from that off-site copy into an empty folder has been tried
      ([Restore](#restore)), and is tried again every few months.
- [ ] An alert fires when the data disk is 80% full. A full disk stops
      SQLite from writing, and sending stops with it.
- [ ] An alert fires when the worker's health check fails, or no new
      backup appears for a day. The dashboard's notification targets also
      hear when a backup fails.

## Health and logs

- `web`: `GET /healthz` answers `{"status": "ok"}` when the app and its
  database answer. Open without a login, and it says nothing else.
- `worker`: `python manage.py worker_status` exits 0 when this container's
  worker wrote its heartbeat within the last minute. That's its Compose
  health check. Docker only reports "unhealthy", so point your monitoring
  at it. From another container: `worker_status --any`.
- The status page in the dashboard («وضعیت سرویس‌ها») shows Kavenegar's
  credit and settings, Shlink, and the worker.

Logs go to stdout as `key=value` lines. **They contain phone numbers.**
Compose caps them at 5 files of 10 MB per service. If you ship them
elsewhere, keep the same short retention and access as the data.

## Stopping and restarting

`docker compose stop` (or a redeploy) is safe during a send:
1. The worker gets SIGTERM.
2. It stops taking new recipients, and lets the requests in flight finish
   and be recorded.
3. The job goes back in the queue and continues on the next start.

Compose waits 90 s (`stop_grace_period`). A request still open after that is
settled with Kavenegar on the next run, never sent again blindly. Don't
shorten the grace period, and don't `kill -9` the worker if you can avoid
it.

## Backups

```bash
docker compose exec -T web python manage.py backup --keep 14
docker compose exec -T web python manage.py verify_backup
```

`backup` copies `app.db`, every campaign DB and the segment files into
`backups/<UTC time>/`, with a `manifest.json` (sizes, SHA-256, row counts):
- It's safe while the dashboard and a send are running: it uses SQLite's
  online backup, not a file copy.
- Each DB copy must pass an integrity check, or the backup fails and keeps
  nothing.
- Older backups are pruned only after a good one, keeping the newest
  `--keep`.

`verify_backup` re-checks the newest one, or a path you give it.

The worker also backs up once a day by itself, at the hour an admin sets on
the backups page (default 09:00 Tehran, keeping the newest 14; it can be
turned off there). One missed while the worker was down runs when it's back.
The backups page and the status page say when it's overdue, and the
notification targets hear when it fails. Prefer your own schedule? Turn the
worker's off and use cron on the host, e.g. at 00:30 UTC (04:00 in Tehran,
outside the sending hours). Both prune to their own count, so don't run both.

```cron
30 0 * * * cd /srv/sms-sender && docker compose exec -T web python manage.py backup --keep 14 && docker compose exec -T web python manage.py verify_backup
```

Also run `backup` right after a campaign finishes sending, and before every
upgrade.

`backups/` is on the same disk as the data. Copy each backup off the
machine, **encrypted** before it leaves (e.g. `age`, `restic`): it holds
every phone number. Keep off-site copies no longer than the business needs
them, and no longer than numbers are kept (see above). Both commands exit
non-zero on failure, so alert on that.

### Restore

```bash
docker compose stop worker web
docker compose run --rm --no-deps web python manage.py verify_backup /app/data/backups/<time>
docker compose run --rm --no-deps web python manage.py restore_backup /app/data/backups/<time> \
    [--replace app.db] [--replace db/<campaign>.db]
docker compose up -d
```

To restore from an off-site copy, decrypt it into `./data/backups/<time>/`
first. Files must belong to uid 1000.

How `restore_backup` behaves:
- It first verifies the backup, and refuses a damaged one.
- It puts back files that are missing.
- It replaces an existing file only if you name it with `--replace`. The
  old file, and its `-wal` / `-shm`, is moved aside as
  `<name>.before-restore-<time>`, never deleted.
- It refuses to touch a campaign DB while a send holds it.

**The one dangerous case:** a campaign DB restored from before a send
doesn't know about the SMS sent after the backup. Sending that campaign
again would reach those people a second time, and `check-sends` can't see
it either, because those records were in the lost file. If a campaign was
sending after the backup was made, don't resume it. Ask the campaign owner.
They can compare it with Kavenegar's records first.

## Upgrades and rollback

```bash
docker compose exec -T web python manage.py backup        # 1. back up
git pull                                                   # 2. new code
export IMAGE_TAG=$(git rev-parse --short HEAD)
docker compose build                                       # 3. build, tagged
docker compose up -d                                       # 4. migrate + restart
```

Prefer a time with no send running (check the campaign pages). A running
send stops cleanly and continues after the restart (see
[Stopping and restarting](#stopping-and-restarting)).

Migrations only go forward:
- The app DB is migrated when `web` starts.
- Each campaign DB is upgraded in place the first time the new version
  opens it.
- An older version refuses a campaign DB written by a newer one ("has
  schema version N … upgrade sms-sender"), rather than misreading it.

**Rollback:**
1. Stop both services.
2. Restore `app.db` from the pre-upgrade backup (`--replace app.db`). This
   loses dashboard changes made since the upgrade.
3. Start the previous image (`IMAGE_TAG=<previous> docker compose up -d`).

Don't roll back a campaign DB that has sent anything since the upgrade:
fix forward instead.

## Runbook

**Did anyone get a campaign twice?**

```bash
docker compose exec worker sms-sender check-sends --campaign <campaign>
```

This counts each phone's SMS from the record of every call to Kavenegar,
with approval tests counted apart. It never sends anything.
- Exit 0: nobody got it twice.
- Exit 1: it lists every phone that got it twice (`TWICE`, with the message
  IDs) or may have (`MAYBE`).

It also reports rows still undecided (`unknown`, `needs_review`).

Campaigns from the sandbox are in `/app/data/sandbox/db/`: use
`--state /app/data/sandbox/db/<campaign>.db`.

**Some recipients are «نامشخص» (unknown).** A request got no clear answer,
so the recipient may have the SMS. Those rows are never resent blindly. Run
reconciliation from the campaign page, or:
`docker compose exec worker sms-sender reconcile --campaign <campaign>`.
It asks Kavenegar and sends nothing.

**Stop all sending, now.** An admin presses «توقف سراسری ارسال‌ها» (Hold
all sending) on the status page («وضعیت سرویس‌ها»). A send pauses at its next heartbeat (seconds),
test SMS on their way are cancelled, nothing new starts, and the command
line refuses to send from the same folder. Every page says so until an
admin lifts the hold there; the sends it paused then continue, and nobody
is sent twice.

**A send stopped with a Kavenegar error.** The campaign page names the code
and what it means, e.g. 418: not enough credit. Fix the cause, then resume
from the page. Recipients already sent are never sent again.

**The worker is unhealthy.** Check `docker compose logs worker`, then
`docker compose restart worker`. A job it was running is picked up again.
After five lost tries it's marked failed.

**The web app doesn't start.** `docker compose logs web`. A missing
`DJANGO_SECRET_KEY` or a failed migration shows there.

**Don't:**
- run a second worker;
- reset sent rows (`sms-sender reset --status sent`);
- delete or move campaign DBs;
- expose the dashboard's port publicly;
- put `SMS_SENDER_SANDBOX=1` on the production data, or remove it from a
  sandbox copy.

## Sandbox drills

A copy with `SMS_SENDER_SANDBOX=1` runs the whole flow with Kavenegar and
Shlink simulated. Use it to rehearse an upgrade, a restart during a send or
a restore, without sending anything. Run it as a separate Compose project
with its own folder and port, never on the production data:

```bash
mkdir /srv/sms-sender-drill && cd /srv/sms-sender-drill
cp /srv/sms-sender/compose.yaml . && mkdir data   # no real data in the drill
# .env: SMS_SENDER_SANDBOX=1, WEB_PORT=8001, a DJANGO_SECRET_KEY of its own,
# IMAGE_TAG of the image to try, and no real API keys
docker compose -p sms-drill up -d
docker compose -p sms-drill exec web python manage.py createsuperuser
```

Compose runs the tagged image if it exists. Build it in the main folder
first: the copied `compose.yaml` can't build, since the source isn't there.

The [README](../README.md#sandbox-try-everything-without-sending) lists
which simulated numbers are rejected or lose their reply.
