# Runbook: the panel on the Mac

What to do when something goes wrong, while the dashboard runs on the Mac
(`./sms-dashboard`). The server has its own: [deploy.md](deploy.md#runbook).

Every send keeps one rule: **a number that was sent is never sent again.**
Pausing, restarting, a crash or a restore never resends to someone who got
the SMS. A number that *may* have got it is marked «نامعلوم» and checked with
Kavenegar first.

All commands run in the repo folder.

## The panel doesn't open

```bash
./sms-dashboard status      # running? the pages and the worker answering?
./sms-dashboard logs        # the last lines; logs -f follows them
./sms-dashboard start       # starts it again
```

It starts at login, and again after a crash (`install-agent`). After a
clean `./sms-dashboard stop` it stays stopped until `start`, or the next
login.

## A send stopped

Open the campaign («کمپین‌ها»). Its step and its history say why.

| What it says | What to do |
|---|---|
| Outside the sending window | Nothing: it goes on by itself when the window opens. |
| Paused («توقف موقت») | «ادامه ارسال» when you're ready. |
| All sending is held | On «وضعیت سرویس‌ها», «برداشتن توقف سراسری». The sends it stopped go on. |
| Kavenegar refused (credit, the key, the account) | Fix the cause (below for credit), then «ادامه ارسال». |
| The Mac restarted or slept | Nothing: the panel comes back by itself. A send that was running goes back to the queue and goes on. Requests that were in flight at the moment of the sleep end «نامعلوم» and are checked with Kavenegar. |

The Mac stays awake by itself while a send or a test SMS runs. A closed lid
still puts a laptop to sleep.

## Credit is low

- «هشدار اعتبار» (system settings) sets the level. Under it, the campaign
  list warns, and the notification targets hear it once.
- Top up at Kavenegar. The panel asks Kavenegar every 15 minutes; «وضعیت
  سرویس‌ها» asks it at once.
- A send never starts when its estimated cost is more than the credit. If
  the credit runs out mid-send, the send stops («Kavenegar refused»): top
  up, then «ادامه ارسال».

## Did anyone get it twice?

On the campaign's report, «آیا کسی دو بار پیامک گرفته است؟». It counts every
call to Kavenegar, not just the recipient's last status. From the CLI:

```bash
.venv/bin/sms-sender check-sends --campaign <short name>   # exit 1 if anyone did
```

- **Twice:** two different SMS reached that number. Tell whoever needs to
  know; the report has the masked number, «جست‌وجوی شماره» the whole story.
- **Maybe:** one SMS went out, and another call's outcome isn't known yet.
  «تطبیق با کاوه‌نگار» on the campaign settles it.

## «نامعلوم» or «نیازمند بررسی» recipients

- **«نامعلوم»** (may have been sent): «تطبیق با کاوه‌نگار» asks Kavenegar what
  it sent. Each one becomes sent, goes back to the queue if Kavenegar never
  got it, or becomes «نیازمند بررسی» if the answer is unclear. A send also
  does this by itself when it starts and when it ends.
- **«نیازمند بررسی»** (Kavenegar's answer was unclear): only an admin can
  queue them again («بازگرداندن به صف»). Do it only when you're sure they
  didn't get the SMS: it's the one way to send to someone twice.

## Restore from a backup

Backups are made every day at 09:00 (Tehran) into `data/backups/`, and the
last 14 are kept. «نسخه‌های پشتیبان» lists them, makes one now and checks one.
They hold every number: keep any copy of them encrypted, never in a
cloud-synced folder.

```bash
./sms-dashboard stop
.venv/bin/python manage.py verify_backup data/backups/<stamp>
.venv/bin/python manage.py restore_backup data/backups/<stamp>        # puts back what's missing
.venv/bin/python manage.py restore_backup data/backups/<stamp> --replace db/<campaign>.db   # replaces one file
./sms-dashboard start
```

- A restore never overwrites by itself: it puts back missing files, and
  replaces one only with `--replace`. The file it replaces is moved aside
  (`.before-restore-<stamp>`), not deleted.
- **A campaign DB from before a send doesn't know about that send.** Don't
  send from a restored campaign until «آیا کسی دو بار پیامک گرفته است؟» and
  «تطبیق با کاوه‌نگار» show who already got it.

## Anything else

- «راهنما» explains every status, button and stop.
- `./sms-dashboard logs` shows the worker's and the pages' errors.
- «سابقه فعالیت‌ها» shows who did what, and when.
