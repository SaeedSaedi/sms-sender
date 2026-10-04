"""Plan 05, P0: the dashboard offers what the CLI can do.

Every CLI command and option, and this app's server commands, has an entry
in sms_sender_web.parity. Every control named there renders on its page
for its role, and not for the role below. A new CLI option fails here
until the dashboard has it, or Saeed has excluded it."""
from __future__ import annotations

from html.parser import HTMLParser

import pytest

pytest.importorskip("django")

import click  # noqa: E402
from django.core.management import get_commands  # noqa: E402
from django.test import Client  # noqa: E402

from sms_sender.cli import cli  # noqa: E402
from sms_sender_web import parity  # noqa: E402
from sms_sender_web.parity import PARITY, Control, Excluded, Implied, Planned  # noqa: E402

from .world import build_world  # noqa: E402

BELOW = {"operator": "viewer", "admin": "operator"}


def _long(option: click.Option) -> str:
    return next(name for name in option.opts if name.startswith("--"))


def cli_keys() -> set[str]:
    keys = {f"sms-sender {_long(o)}" for o in cli.params if isinstance(o, click.Option)}
    for name, command in cli.commands.items():
        keys.add(name)
        keys |= {f"{name} {_long(o)}" for o in command.params if isinstance(o, click.Option)}
    return keys


def server_keys() -> set[str]:
    return {f"manage.py {name}" for name, app in get_commands().items() if app.startswith("sms_sender_web")}


def test_every_cli_command_and_option_has_an_entry():
    missing = sorted(key for key in cli_keys() | server_keys() if parity.entry(key) is None)
    assert not missing, "No dashboard entry (sms_sender_web/parity.py) for: " + ", ".join(missing)


def test_no_entry_names_something_that_does_not_exist():
    known = cli_keys() | server_keys()
    options = {key.split(" ", 1)[1] for key in known if " " in key}
    stale = [key for key in PARITY if key not in known and not (key.startswith("* ") and key[2:] in options)]
    assert not stale, "Entries for commands or options the CLI doesn't have: " + ", ".join(stale)


def test_every_part_says_where_why_or_when():
    pages = {"campaign.new", "campaign.settings", "campaign.fresh", "campaign.approved", "report",
             "status", "account", "suppression", "segment.upload", "segment.map"}
    for key, parts in PARITY.items():
        assert parts, key
        for part in parts:
            if isinstance(part, Control):
                assert part.page in pages and part.role in ("viewer", "operator", "admin"), key
            elif isinstance(part, Planned):
                assert part.phase in parity.PHASES and part.what, key
            else:
                assert isinstance(part, (Implied, Excluded)), key
                assert (part.how if isinstance(part, Implied) else part.why).strip(), key


class _Markers(HTMLParser):
    def __init__(self):
        super().__init__()
        self.keys: set[str] = set()

    def handle_starttag(self, tag, attrs):
        for name, value in attrs:
            if name == "data-cli" and value:
                self.keys |= {key.strip() for key in value.split(",") if key.strip()}


@pytest.mark.django_db
def test_every_control_renders_for_its_role_and_not_below(settings, tmp_path, verified):
    settings.SANDBOX = True  # the status page asks the simulated Kavenegar and Shlink
    settings.DATA_DIR = tmp_path
    settings.SMS_SENDER_DB_DIR = tmp_path / "db"
    world = build_world(tmp_path)

    clients: dict[str, Client] = {}
    for role in ("viewer", "operator", "admin"):
        user = world.users[role]
        client = clients[role] = Client()
        if role == "viewer":
            client.force_login(user)
        else:
            verified(client, user)

    seen: dict[tuple[str, str], set[str]] = {}

    def markers(page: str, role: str) -> set[str]:
        if (page, role) not in seen:
            response = clients[role].get(world.pages[page][0])
            parser = _Markers()
            if response.status_code == 200:
                parser.feed(response.content.decode())
            seen[(page, role)] = parser.keys
        return seen[(page, role)]

    problems = []
    for key, parts in PARITY.items():
        for part in parts:
            if not isinstance(part, Control):
                continue
            if key not in markers(part.page, part.role):
                problems.append(f"{key}: no data-cli control on {part.page} for {part.role}")
            below = BELOW.get(part.role)
            if below and key in markers(part.page, below):
                problems.append(f"{key}: on {part.page}, {below} sees it too")
    assert not problems, "\n".join(problems)
