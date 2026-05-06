"""Wizard tests — questionary is replaced with a queue-of-answers fake."""
from __future__ import annotations

import pytest
from click.testing import CliRunner

from sms_sender import cli as cli_module
from sms_sender import wizard as wizard_module
from sms_sender.cli import cli
from sms_sender.state import SENT, StateStore


# ---------- fake questionary ----------

class _Asker:
    def __init__(self, value):
        self._value = value

    def ask(self):
        return self._value


class FakeQuestionary:
    """Hand-rolled stand-in for the questionary module.

    Each prompt method pops one answer from the queue. Mismatched ordering
    surfaces as an IndexError with a helpful message.
    """

    def __init__(self, answers):
        self._answers = list(answers)
        self._calls: list[tuple[str, str]] = []

    @property
    def Choice(self):
        # Real questionary.Choice has (.title, .value, .checked); we only
        # need a shape that survives being passed around.
        class _C:
            def __init__(self, title, value, checked=False):
                self.title = title
                self.value = value
                self.checked = checked
        return _C

    def _pop(self, kind, label):
        if not self._answers:
            raise IndexError(
                f"FakeQuestionary out of answers at {kind}({label!r}); "
                f"calls so far: {self._calls}"
            )
        v = self._answers.pop(0)
        self._calls.append((kind, label))
        return _Asker(v)

    def select(self, message, choices=None, **_):
        return self._pop("select", message)

    def text(self, message, default="", validate=None, **_):
        return self._pop("text", message)

    def path(self, message, default="", **_):
        return self._pop("path", message)

    def confirm(self, message, default=False, **_):
        return self._pop("confirm", message)

    def checkbox(self, message, choices=None, **_):
        return self._pop("checkbox", message)


def _patch_q(monkeypatch, answers):
    """Make wizard._q() return our fake. Also mark stdin/stdout as TTY."""
    fake = FakeQuestionary(answers)
    monkeypatch.setattr(wizard_module, "_q", lambda: fake)
    monkeypatch.setattr(wizard_module, "_require_tty", lambda: None)
    return fake


# ---------- TTY guard ----------

def test_wizard_requires_tty(monkeypatch):
    """Without a real TTY (CliRunner.invoke), bare `sms-sender` shows help."""
    # The cli() callback checks isatty() for both stdin and stdout — in
    # CliRunner those are pipes, so help is printed instead of the wizard.
    result = CliRunner().invoke(cli, [])
    assert result.exit_code == 0
    assert "Usage:" in result.output


def test_wizard_subcommand_errors_without_tty():
    """Explicit `sms-sender wizard` in a non-TTY context should error clearly."""
    result = CliRunner().invoke(cli, ["wizard"])
    assert result.exit_code != 0
    assert "interactive terminal" in result.output


# ---------- send happy path ----------

def test_wizard_send_happy_path(tmp_path, monkeypatch):
    inp = tmp_path / "in.txt"
    inp.write_text("09120000001\n09120000002\n", encoding="utf-8")
    db = tmp_path / "s.db"
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("KAVENEGAR_API_KEY", "k")

    captured = {}
    def fake_do_send(**kwargs):
        captured.update(kwargs)
    monkeypatch.setattr(cli_module, "_do_send", fake_do_send)

    _patch_q(monkeypatch, answers=[
        # action menu
        "send",
        # file picker
        str(inp),
        # template
        "verify-tpl",
        # tokens checkbox
        ["token"],
        # token value
        "12345",
        # advanced? no
        False,
        # confirm summary? yes
        True,
        # save as profile? no
        False,
    ])
    # Override db_path default by patching _advanced_defaults? Cleaner:
    # check that what we got matches the wizard defaults.
    wizard_module.run_wizard()

    assert captured["input_path"] == str(inp)
    assert captured["template"] == "verify-tpl"
    assert captured["token"] == "12345"
    assert captured["token2"] is None
    assert captured["workers"] == 5
    assert captured["rate"] is None
    assert captured["smoke_test"] is False


def test_wizard_send_loads_profile_then_overrides(tmp_path, monkeypatch):
    inp = tmp_path / "in.txt"
    inp.write_text("09120000001\n", encoding="utf-8")
    cfg = tmp_path / "sms-sender.toml"
    cfg.write_text(
        '[profile.welcome]\n'
        'template = "welcome-tpl"\n'
        'token = "salam"\n',
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("KAVENEGAR_API_KEY", "k")

    captured = {}
    monkeypatch.setattr(cli_module, "_do_send", lambda **kw: captured.update(kw))

    _patch_q(monkeypatch, answers=[
        "send",                # action
        "welcome",             # profile picker → existing
        str(inp),              # file
        "welcome-tpl",         # template (default from profile)
        ["token"],             # token checkbox (token pre-checked from profile)
        "salam",               # token value (default from profile)
        False,                 # no advanced
        True,                  # confirm
        False,                 # don't save
    ])
    wizard_module.run_wizard()
    assert captured["template"] == "welcome-tpl"
    assert captured["token"] == "salam"


def test_wizard_send_user_can_skip_profile(tmp_path, monkeypatch):
    inp = tmp_path / "in.txt"
    inp.write_text("09120000001\n", encoding="utf-8")
    cfg = tmp_path / "sms-sender.toml"
    cfg.write_text(
        '[profile.foo]\ntemplate = "ignored"\n',
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("KAVENEGAR_API_KEY", "k")
    monkeypatch.setattr(cli_module, "_do_send", lambda **_: None)

    captured = {}
    monkeypatch.setattr(cli_module, "_do_send", lambda **kw: captured.update(kw))

    _patch_q(monkeypatch, answers=[
        "send",
        "__new__",            # skip profile
        str(inp),
        "fresh-tpl",
        [],                   # no tokens
        False,                # no advanced
        True,                 # confirm
        False,                # no save
    ])
    wizard_module.run_wizard()
    assert captured["template"] == "fresh-tpl"
    assert captured["token"] is None


def test_wizard_send_aborts_when_user_declines_confirm(tmp_path, monkeypatch):
    inp = tmp_path / "in.txt"
    inp.write_text("09120000001\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("KAVENEGAR_API_KEY", "k")

    called = {"n": 0}
    monkeypatch.setattr(cli_module, "_do_send", lambda **_: called.__setitem__("n", called["n"] + 1))

    _patch_q(monkeypatch, answers=[
        "send", str(inp), "tpl", [], False,
        False,  # confirm summary? NO
    ])
    wizard_module.run_wizard()
    assert called["n"] == 0


# ---------- DB peek ----------

def test_wizard_peek_state_counts_already_sent(tmp_path, monkeypatch):
    db = tmp_path / "s.db"
    store = StateStore(db)
    store.upsert_pending([("09120000001", "09120000001")])
    with store._tx() as conn:
        conn.execute("UPDATE recipients SET status=? WHERE phone=?", (SENT, "09120000001"))

    from sms_sender.input_loader import LoadedRow
    new, sent, failed = wizard_module._peek_state(
        str(db),
        [LoadedRow(phone="09120000001", raw="x"),
         LoadedRow(phone="09120000099", raw="y")],
    )
    assert new == 1
    assert sent == 1
    assert failed == 0


def test_wizard_peek_state_missing_db_returns_all_new(tmp_path):
    from sms_sender.input_loader import LoadedRow
    new, sent, failed = wizard_module._peek_state(
        str(tmp_path / "missing.db"),
        [LoadedRow(phone="09120000001", raw="x")],
    )
    assert new == 1 and sent == 0 and failed == 0


def test_state_status_for_phones_chunks_at_500(tmp_path):
    db = tmp_path / "s.db"
    store = StateStore(db)
    rows = [(f"0912{i:07d}", "x") for i in range(600)]
    store.upsert_pending(rows)
    statuses = store.status_for_phones(p for p, _ in rows)
    assert len(statuses) == 600


# ---------- profile save ----------

def test_save_profile_writes_section_no_secrets(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    advanced = wizard_module._advanced_defaults({})
    advanced["rate"] = "10/s"
    advanced["notify_target"] = None
    wizard_module._save_profile(
        name="myverify",
        input_path="./numbers.txt",
        template="verify-tpl",
        tokens={"token": "12345"},
        advanced=advanced,
    )
    out = (tmp_path / "sms-sender.toml").read_text(encoding="utf-8")
    assert "[profile.myverify]" in out
    assert 'template = "verify-tpl"' in out
    assert 'token = "12345"' in out
    assert 'rate = "10/s"' in out
    # API key never persisted in any form.
    assert "KAVENEGAR" not in out
    assert "api_key" not in out


def test_save_profile_refuses_to_clobber(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    cfg = tmp_path / "sms-sender.toml"
    cfg.write_text("[profile.dup]\ntemplate = \"old\"\n", encoding="utf-8")
    advanced = wizard_module._advanced_defaults({})
    wizard_module._save_profile(
        "dup", "./n.txt", "new-tpl", {}, advanced
    )
    after = cfg.read_text(encoding="utf-8")
    assert "new-tpl" not in after
    assert "Skipping save" in capsys.readouterr().out


def test_save_profile_quotes_special_chars(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    advanced = wizard_module._advanced_defaults({})
    advanced["notify_target"] = 'http://example.com/"weird"'
    wizard_module._save_profile(
        "esc", "./n.txt", "t", {}, advanced
    )
    # Re-read with tomllib to confirm it parses.
    try:
        import tomllib
    except ModuleNotFoundError:
        import tomli as tomllib  # type: ignore
    data = tomllib.loads((tmp_path / "sms-sender.toml").read_text(encoding="utf-8"))
    assert data["profile"]["esc"]["notify"] == 'http://example.com/"weird"'


# ---------- simple actions ----------

def test_wizard_status_action(tmp_path, monkeypatch, capsys):
    db = tmp_path / "s.db"
    store = StateStore(db)
    store.upsert_pending([("09120000001", "x")])
    monkeypatch.chdir(tmp_path)

    _patch_q(monkeypatch, answers=["status", str(db)])
    wizard_module.run_wizard()
    out = capsys.readouterr().out
    assert "pending" in out


def test_wizard_purge_declined_keeps_files(tmp_path, monkeypatch):
    db = tmp_path / "s.db"
    StateStore(db)  # creates the file
    assert db.exists()
    monkeypatch.chdir(tmp_path)

    _patch_q(monkeypatch, answers=[
        "purge",
        str(db),
        False,  # confirm? no
    ])
    wizard_module.run_wizard()
    assert db.exists()


def test_wizard_dry_run_action(tmp_path, monkeypatch, capsys):
    inp = tmp_path / "in.txt"
    inp.write_text("09120000001\n09120000002\nbadnumber\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    _patch_q(monkeypatch, answers=["dry-run", str(inp)])
    wizard_module.run_wizard()
    out = capsys.readouterr().out
    assert "valid=2" in out
    assert "INVALID" in out


# ---------- API key guard ----------

def test_wizard_send_aborts_when_api_key_missing(tmp_path, monkeypatch):
    inp = tmp_path / "in.txt"
    inp.write_text("09120000001\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("KAVENEGAR_API_KEY", raising=False)
    _patch_q(monkeypatch, answers=["send"])

    with pytest.raises(Exception) as exc_info:
        wizard_module.run_wizard()
    assert "KAVENEGAR_API_KEY" in str(exc_info.value)


# ---------- list_profiles helper ----------

def test_list_profiles_orders_default_first(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "sms-sender.toml").write_text(
        '[profile.zzz]\nworkers = 1\n'
        '[profile.default]\nworkers = 5\n'
        '[profile.aaa]\nworkers = 2\n',
        encoding="utf-8",
    )
    from sms_sender.profile import list_profiles
    names = list_profiles()
    assert names[0] == "default"
    assert set(names) == {"default", "zzz", "aaa"}


def test_list_profiles_no_config(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    from sms_sender.profile import list_profiles
    assert list_profiles() == []
