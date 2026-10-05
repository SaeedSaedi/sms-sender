"""Where the dashboard offers each CLI capability (plan 05, P0).

Every `sms-sender` command and option, and this app's own server commands,
has an entry here. `tests/web/test_parity.py` fails when the CLI gains a
command or option without one, and checks that every control named here
really renders, on its page, for its role (and not for the role below).

An entry is a tuple of parts, and every part holds:

- Control(page, role): a control on that page carries data-cli="<key>"
  (several keys are separated by commas). `page` names a page state the
  test knows how to build.
- Implied(how): covered without a control of its own.
- Excluded(why): deliberately not in the dashboard. Saeed approved these on
  2026-10-04 (plan 05, section 2.3, and decision 7).
- Planned(phase, what): not there yet. The P6 gate allows none.

Lookup order for "<command> <option>": the exact key; then the same option
of `send`, for commands whose options mean the same as send's (ALIASES);
then the options every command shares ("* --campaign", …).
"""
from __future__ import annotations

from dataclasses import dataclass

VIEWER, OPERATOR, ADMIN = "viewer", "operator", "admin"


@dataclass(frozen=True)
class Control:
    page: str
    role: str = OPERATOR
    note: str = ""


@dataclass(frozen=True)
class Implied:
    how: str


@dataclass(frozen=True)
class Excluded:
    why: str


@dataclass(frozen=True)
class Planned:
    phase: str
    what: str


Part = Control | Implied | Excluded | Planned

PHASES = ("P3", "P4", "P5")  # P2 is done: nothing may be planned for it again
# Commands whose options are send's, with the same meaning.
ALIASES = {"retry-failed": "send", "dry-run": "send", "preview": "send"}

_LOGS = Excluded("Logs are for DevOps (docker logs); the dashboard shows each job's result.")
_ADVANCED = Control("campaign.settings", ADMIN, note="advanced settings")

PARITY: dict[str, tuple[Part, ...]] = {
    # ---------- commands ----------
    "send": (Control("campaign.approved"),),
    "retry-failed": (Control("campaign.completed", note="not sent: send to them again; rejected: queue again"),),
    "status": (Control("report", VIEWER),),
    "export-failed": (Control("report", note="rejected and invalid rows; any status through the filters"),),
    "reset": (Control("campaign.completed", note="queue again, by status, each with its role"),),
    "purge": (Control("campaign.completed.admin", ADMIN, note="a typed confirmation and a backup first"),),
    "reconcile": (Control("campaign.approved"),),
    "delivery": (Control("campaign.approved"),),
    "check-sends": (Control("report", VIEWER),),
    "clicks": (Control("campaign.approved"),),
    "export-attribution": (Control("report"),),
    "export-clickers": (Control("report"),),
    "dry-run": (Control("campaign.fresh", note="the check, each row's tokens and the link's request"),),
    "preview": (Control("campaign.fresh", note="each recipient's message"),),
    "wizard": (Excluded("The dashboard is the guided flow."),),

    # ---------- send (and its aliases) ----------
    "send --input": (Control("campaign.settings"),),
    "send --template": (Control("campaign.settings"),),
    "send --token": (Control("campaign.settings"),),
    "send --token2": (Control("campaign.settings"),),
    "send --token3": (Control("campaign.settings"),),
    "send --token10": (Control("campaign.settings"),),
    "send --token20": (Control("campaign.settings"),),
    "send --token-column": (Control("campaign.settings"),),
    "send --value-map": (Control("campaign.settings"),),
    "send --user-id-column": (Control("segment.map"),),
    "send --segment": (Control("segment.upload"), Control("campaign.completed", note="another round, another segment")),
    "send --workers": (Control("campaign.settings"),),
    "send --max-attempts": (_ADVANCED,),
    "send --timeout": (_ADVANCED,),
    "send --backoff-max": (_ADVANCED,),
    "send --campaign": (Control("campaign.new"),),
    "send --opt-out": (Control("suppression", note="numbers or a file, for every campaign or one"),),
    "send --send-window": (
        Control("campaign.settings"),
        Excluded("'off' isn't offered: prohibited hours are a decided compliance rule."),
    ),
    "send --allow-settings-change": (Control("campaign.halted", ADMIN, note="a typed confirmation, a backup first"),),
    "send --log-file": (_LOGS,),
    "send --verbose": (_LOGS,),
    "send --quiet": (_LOGS,),
    "send --smoke-test": (Control("campaign.approved"),),
    "send --approval-test": (Control("campaign.fresh", note="always required"),),
    "send --test-number": (Control("account"),),
    "send --no-preflight": (
        Excluded("A safety bypass for offline development: the dashboard always checks the account."),
    ),
    "send --rate": (Control("campaign.settings"),),
    "send --frequency-cap": (Control("system", ADMIN, note="for every campaign; off until an admin sets it"),),
    "send --notify": (Planned("P5", "notifications"),),
    "send --link-url": (Control("campaign.settings"),),
    "send --link-token": (Control("campaign.settings"),),
    "send --link-format": (Control("campaign.settings"),),
    "send --link-strategy": (Control("campaign.settings"),),
    "send --link-expiry-days": (Control("campaign.settings"),),
    "send --link-rate": (_ADVANCED,),
    "send --utm-source": (Control("campaign.settings"),),
    "send --utm-medium": (Control("campaign.settings"),),
    "send --utm-campaign": (Control("campaign.settings"),),
    "send --utm-content": (Control("campaign.settings"),),

    # ---------- options of their own ----------
    "retry-failed --include-permanent": (Control("campaign.completed"),),
    "reconcile --min-age": (Control("campaign.completed"),),
    "reconcile --requeue-not-found": (Control("campaign.completed"),),
    "reset --status": (Control("campaign.completed"),),
    "preview --phone": (Control("campaign.fresh"),),
    "preview --limit": (Control("campaign.fresh"),),
    "preview --check-account": (Control("status", VIEWER),),
    "preview --send": (
        Control("campaign.fresh", note="the test SMS"),
        Excluded("Only to your own number (decision 7, 2026-10-04)."),
    ),
    "sms-sender --config": (Control("campaign.completed", note="duplicate a campaign: its settings are the preset"),),
    "sms-sender --profile": (Control("campaign.completed", note="duplicate a campaign: its settings are the preset"),),

    # ---------- options every command shares ----------
    "* --campaign": (Implied("Every page and action belongs to one campaign."),),
    "* --state": (Implied("Each campaign has its own records, named by its short name."),),
    "* --log-file": (_LOGS,),
    "* --timeout": (
        Control("campaign.settings", ADMIN, note="the campaign's timeout; its follow-up jobs use it too"),
    ),
    "* --out": (Implied("The browser saves the download."),),
    "* --yes": (Implied("A confirmation dialog takes its place."),),

    # ---------- this app's server commands ----------
    "manage.py backup": (Control("backups", ADMIN),),
    "manage.py verify_backup": (Control("backups", ADMIN),),
    "manage.py restore_backup": (
        Excluded("Needs both services stopped: an ops procedure (docs/deploy.md)."),
    ),
    "manage.py worker_status": (Control("status", VIEWER),),
    "manage.py run_worker": (Implied("The worker service runs it."),),
}


def entry(key: str) -> tuple[Part, ...] | None:
    """The parts covering "<command>" or "<command> <option>", or None."""
    if key in PARITY:
        return PARITY[key]
    command, _, option = key.partition(" ")
    if not option:
        return None
    alias = ALIASES.get(command)
    if alias and f"{alias} {option}" in PARITY:
        return PARITY[f"{alias} {option}"]
    return PARITY.get(f"* {option}")


def planned() -> dict[str, Planned]:
    """What's still missing, by key."""
    return {
        key: part for key, parts in PARITY.items() for part in parts if isinstance(part, Planned)
    }
