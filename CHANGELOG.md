# Changelog

Each entry names the pull requests it came from.

## 1.0.0 (unreleased)

The CLI became a whole campaign tool, and a Persian dashboard runs it on a Mac or a server.

### Never send twice
- One process per campaign DB (a run lock). A request that may have reached Kavenegar is never resent: it's `unknown` until Kavenegar is asked what it sent. Every call is recorded (#1).
- Reconciliation asks Kavenegar per day, and trusts "not found" only for fresh calls (#7).
- "Did anyone get it twice?": `check-sends` and the report's panel count every call (#17, #25).
- Every campaign-DB commit reaches the disk (`synchronous=FULL`), so a power cut can't undo a send's record (#59).
- Tested against crashes at each step of a send, a full disk and a sleeping Mac. The Mac stays awake during sends (#61).

### Campaigns from the CLI
- Campaign identity and settings, input validation, checks before sending, delivery reports (#2, #3).
- Short links per recipient, made before any SMS; user IDs and segments; clicks and exports (#4).

### The dashboard
- **Foundation:** sign-in with two-step verification, roles, the activity log, Docker (#5, #9, #16).
- **Campaigns:**
  - the worker and its jobs (#8, #40);
  - segments and the suppression list (#10);
  - the steps of a campaign: check, test SMS and approval, send (#11);
  - reports and downloads (#12);
  - the sandbox, to try everything without sending (#13, #15).
- **Plan 05, parity with the CLI:**
  - every CLI option has its control (#18);
  - the design system (#19);
  - the campaign lifecycle (#20);
  - settings, templates and the preview (#21);
  - scheduling (#22);
  - rounds and duplicates (#23);
  - the recipients tab (#24);
  - queue again (#25);
  - analytics (#26);
  - audiences and conversions (#27);
  - the attribution API (#28);
  - segment actions (#29);
  - the frequency cap (#30);
  - backups, purge and adopting CLI campaigns (#31);
  - notifications (#32);
  - users (#33);
  - performance and accessibility (#34);
  - browser journeys and help (#35, #36).
- **Review (R1–R5):**
  - tests never touch real data (#39);
  - two worker lanes (#40);
  - design v2 (#41);
  - one send for several segments, importing the CLI's profiles (#42);
  - hold all sending, find a number, the credit warning (#43).
- **Local first (plan 06):**
  - `./sms-dashboard` runs it on a Mac (#44);
  - presets and a new alert in one page (#46, #47);
  - the control room, notifications, Ctrl+K and the calendar (#48, #49);
  - insights (#50);
  - design v3, validation as you type and undo (#51, #52);
  - moving to the server (#53);
  - test SMS to the team and a second approver (#55);
  - phone numbers kept 12 months (#56).

### Security and privacy
- Secrets scrubbed from every log line; downloads safe to open in Excel; data and logs readable by their owner only; notification targets limited (#61).

### Server
- The image installs locked, hash-checked packages (#60) and its own SQLite without the WAL-reset bug (#61). The DevOps guide is `docs/deploy.md` (#6, #17, #53, #58).

### Documents
- The README starts with daily use; a runbook for the Mac (`docs/runbook.md`); this changelog (#62).

### Fixes
- The status page with an account that never expires (#37).
- "Internal Server Error" from gunicorn's sync workers (#38).
- The sending window: 24:00 is midnight (#45).
- The status page's layout (#54).
- Timing budgets on CI (#57).
- Telegram targets with a real bot token (#61).
- Clicks per Tehran hour (#61).

## 0.1.0 (2026-07-27)

The CLI: bulk SMS through Kavenegar's `verify/lookup`, with a state DB, retries and resumable runs.
